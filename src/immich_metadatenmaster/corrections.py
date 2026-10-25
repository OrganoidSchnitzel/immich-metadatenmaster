"""Check & fix: mistakes in the dates and locations already in Immich, found by comparing photos with GPX
tracks and with phone photos of the same moments, and a suggested fix for each.

The checks, per trip (the GPX tracks a few hours apart, and/or a time range):

- clock:       a camera's clock was off → correct the capture times of its photos
- move_track:  a location does not fit the track at the photo's (corrected) time → move it there
- move_nearby: a location placed by hand is far from the phone photos of the same minutes → move it
- local_time:  photos without a time zone are shown in the camera's home time instead of the local
               time of the place → set the local time
- add_track:   a photo without a location fits a track (once its clock is corrected) → add it

Nothing is written until the user accepts a fix; every fix is logged in the History and can be undone.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from . import clocks
from .config import Settings
from .geotag import (MAX_ASSETS, RELOCATE_MIN_M, SOURCES, _TZ_SLACK, GeoTagger, _Located, _format_offset, lacks_zone,
                     looks_like_screenshot, photo_instant, place_zone, usable_offset)
from .gpx import Track, TrackPoint, distance_m
from .history import History, iana_name, zone_of
from .immich import Asset, ImmichClient
from .service import tag_assets
from .store import Store

Progress = Callable[[int, int], None]
TABLE = "correction"
RUN_KEY = "check_run"
# Tracks closer together than this belong to one trip.
TRIP_GAP = timedelta(hours=12)
# A location placed by hand this far from the phone photos of the same minutes is probably wrong.
FAR_FROM_PHONE_M = 1000.0
NEARBY_WINDOW = timedelta(minutes=10)
# A shown time this far from the local time of the place is worth correcting.
MIN_LOCAL_DIFF = timedelta(minutes=1)
DATE_KINDS = {"clock", "local_time"}
KIND_ORDER = ["clock", "move_track", "move_nearby", "local_time", "add_track"]


def _noop(done: int, total: int) -> None:
    pass


@dataclass
class CheckOptions:
    track_ids: list[str] | None = None  # None: all tracks
    use_tracks: bool = True
    start: datetime | None = None
    end: datetime | None = None
    # The zone camera clocks were set to, for photos that store none.
    camera_tz: str = "UTC"
    tolerance_min: float = 10
    max_gap_min: float = 15

    def to_dict(self) -> dict[str, Any]:
        return {**{k: v for k, v in self.__dict__.items() if k not in {"start", "end"}},
                "start": self.start.isoformat() if self.start else None,
                "end": self.end.isoformat() if self.end else None}


@dataclass
class _Trip:
    start: datetime
    end: datetime
    names: list[str]

    @property
    def label(self) -> str:
        names = [n for n in self.names if n]
        if not names:
            return "Time range"
        return ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")


def _km(m: float) -> str:
    return f"{m / 1000:.1f} km" if m >= 1000 else f"{round(m)} m"


def _duration(seconds: int) -> str:
    """7252 → 2 h 00 min, 1800 → 30 min, 125 → 2 min 05 s"""
    h, rest = divmod(abs(int(seconds)), 3600)
    m, s = divmod(rest, 60)
    # Seconds only for small offsets (an hour's is measuring noise).
    parts = ([f"{h} h"] if h else []) + [f"{m:02d} min" if h else f"{m} min"] + ([f"{s:02d} s"] if s and not h else [])
    return " ".join(parts)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


class Corrections:
    def __init__(self, settings: Settings, immich: ImmichClient, store: Store, geo: GeoTagger) -> None:
        self.settings = settings
        self.immich = immich
        self.store = store
        self.geo = geo
        self.history = History(store, immich)

    # --- checking ------------------------------------------------------------------------------

    def scan(self, opts: CheckOptions, progress: Progress = _noop) -> dict[str, Any]:
        """Run all checks and store their findings (replacing the previous ones). Writes nothing to Immich."""
        tol = timedelta(minutes=opts.tolerance_min)
        tracks = self.geo._load_tracks(opts.track_ids) if opts.use_tracks else []
        trips = self._trips(tracks, opts, tol)
        skip = {item["id"] for item in self.store.no_location()}
        findings: dict[str, dict[str, Any]] = {}
        fixes: dict[str, dict[str, Any]] = {}
        trip_info = []
        for n, trip in enumerate(trips):
            in_trip = [t for t in tracks if t[2].end + tol >= trip.start and t[2].start - tol <= trip.end]
            trip_info.append(self._check_trip(n, trip, in_trip, opts, skip, findings, fixes))
            progress(n + 1, len(trips))
        run = {
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "options": opts.to_dict(), "trips": trip_info,
            "findings": sorted(findings.values(), key=lambda f: (f["trip"], KIND_ORDER.index(f["kind"]), f["camera"])),
        }
        self.store.save_suggestions(TABLE, fixes, replace_all=True)
        self.store.set_value(RUN_KEY, run)
        return self.results()

    @staticmethod
    def _trips(tracks: list[tuple[str, str, Track]], opts: CheckOptions, tol: timedelta) -> list[_Trip]:
        spans: list[tuple[datetime, datetime, str]] = [(t.start - tol, t.end + tol, name) for _, name, t in tracks]
        if opts.start and opts.end:
            if opts.end <= opts.start:
                raise ValueError("The end of the time range is before its start.")
            spans.append((opts.start, opts.end, ""))
        if not spans:
            raise ValueError("Select GPX tracks with times, or enter a time range.")
        trips: list[_Trip] = []
        for start, end, name in sorted(spans):
            if trips and start - trips[-1].end <= TRIP_GAP:
                trips[-1].end = max(trips[-1].end, end)
                trips[-1].names.append(name)
            else:
                trips.append(_Trip(start, end, [name]))
        return trips

    def _check_trip(self, n: int, trip: _Trip, tracks: list[tuple[str, str, Track]], opts: CheckOptions,
                    skip: set[str], findings: dict[str, dict[str, Any]], fixes: dict[str, dict[str, Any]]
                    ) -> dict[str, Any]:
        cam_tz = ZoneInfo(opts.camera_tz)
        tol, max_gap = timedelta(minutes=opts.tolerance_min), timedelta(minutes=opts.max_gap_min)
        # A day around the trip: phone photos of the same moments, whatever the cameras' clocks say.
        span = timedelta(seconds=clocks.SPAN_S) + _TZ_SLACK
        wide = {a.id: a for a in self.immich.assets_taken_between(trip.start - span, trip.end + span) if a.shown}
        if len(wide) > MAX_ASSETS:
            raise ValueError(f"More than {MAX_ASSETS} photos around “{trip.label}”. Check fewer tracks at once.")
        near = {aid for aid, a in wide.items()
                if a.taken_at and trip.start - _TZ_SLACK <= a.taken_at <= trip.end + _TZ_SLACK}
        found = self.geo.estimate_clocks([(trip.start, trip.end)], cam_tz, True, only_ids=near, assets=wide)
        checked = {c["camera"]: c for c in found["cameras"]}
        verified = {name: off for name, c in checked.items() if (off := usable_offset(c)) is not None}

        entries: list[tuple[Asset, datetime, bool]] = []
        for a in wide.values():
            raw = photo_instant(a, cam_tz, True)
            if raw is None or a.local_datetime is None:
                continue
            phone = clocks.is_phone(a.camera_make, a.camera_model)
            t = raw + timedelta(seconds=0 if phone else verified.get(a.camera, 0))
            entries.append((a, t, phone))
        located = sorted(((t.timestamp(), a) for a, t, _ in entries if a.has_location), key=lambda x: x[0])
        placed = self.geo._placed_by_hand(located)
        phone_gps = _Located()
        for ts, a in located:
            if clocks.is_phone(a.camera_make, a.camera_model) and a.id not in placed:
                phone_gps.instants.append(ts)
                phone_gps.assets.append(a)
        recorded = [t for t in tracks if not t[2].assumed_times]

        def add(kind: str, a: Asset, after: dict[str, Any], **extra: Any) -> None:
            fid = f"{n}:{kind}:{a.camera}"
            findings.setdefault(fid, {"id": fid, "trip": n, "kind": kind, "camera": a.camera, "values": []})
            if "distance_m" in extra:
                findings[fid]["values"].append(extra["distance_m"])
            fixes[f"{fid}|{a.id}"] = {
                "finding": fid, "asset_id": a.id, "file_name": a.file_name, "type": a.type, "camera": a.camera,
                "before": {"local": a.local_datetime.isoformat() if a.local_datetime else None,
                           "time_zone": a.time_zone, "lat": a.latitude, "lon": a.longitude},
                "after": after, **extra}

        for a, t, phone in sorted(entries, key=lambda e: e[1]):
            if not trip.start <= t <= trip.end:
                continue
            position = (a.latitude, a.longitude) if a.has_location else None
            if a.id not in skip:
                new = self._location_fix(a, t, phone, placed, tracks, recorded, phone_gps, tol, max_gap)
                if new is not None:
                    kind, after, extra = new
                    add(kind, a, after, **extra)
                    position = (after["lat"], after["lon"])
            if phone or a.type != "IMAGE" or a.camera not in verified:
                continue  # phones set their own clocks; a camera without a checked clock is left alone
            if verified[a.camera]:
                add("clock", a, self._date_after(a, t, position, opts.camera_tz), offset_s=verified[a.camera])
            elif lacks_zone(a.time_zone) and position and (zone := place_zone(*position)):
                wall = t.astimezone(ZoneInfo(zone)).replace(tzinfo=None)
                if abs(wall - a.local_datetime) >= MIN_LOCAL_DIFF:  # type: ignore[operator]
                    add("local_time", a, self._date_after(a, t, position, opts.camera_tz))

        phones = {a.camera for a, _, phone in entries if phone}
        for f in findings.values():
            if f["trip"] == n:
                f.update(self._describe(f, checked.get(f["camera"]), f["camera"] in phones, opts.camera_tz,
                                        [x for x in fixes.values() if x["finding"] == f["id"]]))
        cameras = []
        for name, c in sorted(checked.items(), key=lambda x: -x[1]["photos"]):
            cameras.append({"camera": name, "photos": c["photos"], "offset_s": verified.get(name),
                            "confidence": c["confidence"], "moments": c["moments"]})
        return {"label": trip.label, "start": trip.start.isoformat(), "end": trip.end.isoformat(),
                "photos": len(wide), "cameras": cameras, "references": found["references"],
                "smart_search": found["smart_search"]}

    def _location_fix(self, a: Asset, t: datetime, phone: bool, placed: set[str],
                      tracks: list[tuple[str, str, Track]], recorded: list[tuple[str, str, Track]],
                      phone_gps: _Located, tol: timedelta, max_gap: timedelta
                      ) -> tuple[str, dict[str, Any], dict[str, Any]] | None:
        if not a.has_location:
            if looks_like_screenshot(a) or not tracks:
                return None
            match = GeoTagger._on_tracks(t, tracks, tol, max_gap)
            if match is None:
                return None
            return "add_track", self._place(match), {}
        if phone and a.id not in placed:
            return None  # the phone's own GPS
        here = TrackPoint(a.latitude, a.longitude)  # type: ignore[arg-type]
        # A position set by hand is worse than even a planned route with times entered by the user.
        usable = tracks if a.id in placed else recorded
        match = GeoTagger._on_tracks(t, usable, tol, max_gap) if usable else None
        if match is not None:
            d = distance_m(here, TrackPoint(match["lat"], match["lon"]))
            return ("move_track", self._place(match), {"distance_m": round(d)}) if d >= RELOCATE_MIN_M else None
        if a.id in placed:
            match = GeoTagger._neighbours(t, phone_gps, NEARBY_WINDOW)
            if match is not None:
                d = distance_m(here, TrackPoint(match["lat"], match["lon"]))
                if d >= FAR_FROM_PHONE_M:
                    return "move_nearby", self._place(match), {"distance_m": round(d)}
        return None

    @staticmethod
    def _place(match: dict[str, Any]) -> dict[str, Any]:
        return {"lat": match["lat"], "lon": match["lon"], "source": match["source"], "detail": match["detail"],
                "time_diff_s": match["time_diff_s"]}

    @staticmethod
    def _date_after(a: Asset, t: datetime, position: tuple[float, float] | None, camera_tz: str) -> dict[str, Any]:
        """The time to write: in the photo's own zone, else in the zone of the place (else the camera's)."""
        if not lacks_zone(a.time_zone):
            zone, name = zone_of(a.time_zone), iana_name(a.time_zone)
        else:
            name = (place_zone(*position) if position else None) or camera_tz
            zone = ZoneInfo(name)
        when = t.astimezone(zone)  # type: ignore[arg-type]
        return {"date": when.isoformat(timespec="milliseconds"), "time_zone": name,
                "local": when.replace(tzinfo=None).isoformat(timespec="seconds")}

    @staticmethod
    def _describe(f: dict[str, Any], clock: dict[str, Any] | None, phone: bool, camera_tz: str,
                  rows: list[dict[str, Any]]) -> dict[str, Any]:
        """Title, explanation and confidence of a finding."""
        count, cam = len(rows), f["camera"] or "Unknown camera"
        photos = _plural(count, "photo")
        # Is the time the photos are compared at right? Phones set their clocks; cameras need the check.
        sure = phone or (clock is not None and usable_offset(clock) is not None)
        out: dict[str, Any] = {"confidence": "high" if sure else "low"}
        distances = f.pop("values")
        if f["kind"] == "clock":
            off = rows[0]["offset_s"]
            behind = "behind" if off > 0 else "ahead"
            evidence = f"{clock['moments']} moments also photographed with a phone"  # type: ignore[index]
            if clock.get("pairs_agreeing"):  # type: ignore[union-attr]
                evidence += f", {clock['pairs_agreeing']} similar-looking photo pairs"  # type: ignore[index]
            same_wall = all(r["after"]["local"][:16] == (r["before"]["local"] or "")[:16] for r in rows)
            out.update(
                title=f"{cam}: clock {_duration(off)} {behind}", offset_s=off,
                confidence=clock["confidence"],  # type: ignore[index]
                examples=clock.get("examples", []),  # type: ignore[union-attr]
                text=(f"Found from {evidence}. " + (
                    "The camera was set to the local time: the times shown stay, Immich gets the right time zone, "
                    "so its photos sort correctly between your phone's." if same_wall else
                    f"Correcting moves its {photos} to the right time (for photos without a time zone, in the "
                    "local time of the place).")))
        elif f["kind"] == "move_track":
            out.update(title=f"{cam}: {_plural(count, 'location')} don't fit the GPX track",
                       text=f"At the photos' {'corrected ' if clock and usable_offset(clock) else ''}time the track "
                            f"was elsewhere (median {_km(statistics.median(distances))} away). Moving puts them on "
                            "the track." + ("" if sure else
                                            " The camera's clock could not be checked, so this is less certain."))
        elif f["kind"] == "move_nearby":
            out.update(title=f"{cam}: {_plural(count, 'location')} placed by hand, far from your phone photos",
                       text=f"Your phone was {_km(statistics.median(distances))} away (median) in the same minutes. "
                            "Moving puts them where the phone was.")
            out["confidence"] = "medium"
        elif f["kind"] == "local_time":
            zones = sorted({r["after"]["time_zone"] for r in rows})
            out.update(title=f"{cam}: {photos} shown in {camera_tz} time",
                       text=f"Taken in {', '.join(zones)}, but the camera stores no time zone, so Immich shows its "
                            f"home time ({camera_tz}). Setting the local time shows when it was there; the order of "
                            "the photos stays the same.")
        else:
            out.update(title=f"{cam}: {photos} without a location fit a GPX track",
                       text="They get the position on the track at their " +
                            ("corrected time." if clock and usable_offset(clock) else "time.") +
                            ("" if sure else " The camera's clock could not be checked (no phone photos of the same "
                                             "moments): look at a few before adding."))
            if not sure:
                out["confidence"] = "medium"
        return out

    # --- reviewing and fixing ------------------------------------------------------------------

    def results(self) -> dict[str, Any] | None:
        run = self.store.get_value(RUN_KEY)
        if run is None:
            return None
        rows = self.store.suggestions(TABLE)
        by_finding: dict[str, list[dict[str, Any]]] = {}
        for row in rows.values():
            by_finding.setdefault(row["finding"], []).append(row)
        findings = []
        for f in run["findings"]:
            items = by_finding.get(f["id"], [])
            if items:
                findings.append({**f, "count": len(items), "items": items})
        return {**run, "findings": findings}

    def apply(self, finding_id: str, asset_ids: list[str], progress: Progress = _noop) -> dict[str, Any]:
        run = self.store.get_value(RUN_KEY) or {}
        finding = next((f for f in run.get("findings", []) if f["id"] == finding_id), None)
        if finding is None:
            raise ValueError("This finding is gone. Check again.")
        keys = [f"{finding_id}|{aid}" for aid in asset_ids]
        rows = self.store.suggestions(TABLE, keys)
        old = self.history.snapshots([r["asset_id"] for r in rows.values()])
        kind = finding["kind"]
        changes: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = []
        done: list[str] = []
        try:
            for i, (key, r) in enumerate(rows.items()):
                aid, after = r["asset_id"], r["after"]
                name, before = old.get(aid, ("", {}))
                if kind in DATE_KINDS:
                    self.immich.set_date([aid], after["date"], after["time_zone"])
                    new = {"date": after["date"], "time_zone": after["time_zone"]}
                else:
                    self.immich.set_location([aid], after["lat"], after["lon"])
                    new = {"latitude": after["lat"], "longitude": after["lon"], "source": after["source"],
                           "detail": after.get("detail")}
                changes.append((aid, name or r["file_name"], before, new))
                done.append(key)
                progress(i + 1, len(rows))
        finally:
            # Also after an error part-way: log what was written, so it can be undone and is not written twice.
            if kind == "clock":
                self.history.log("date", "clock", f"Clock of {finding['camera']} corrected by "
                                 f"{_format_offset(finding['offset_s'])}", None, changes)
            elif kind == "local_time":
                self.history.log("date", "localtime", "Local time of the place", None, changes)
            else:
                for source in sorted({r["after"]["source"] for r in rows.values()}):
                    label, tag = SOURCES.get(source, (source, f"Location/{source}"))
                    mine = [c for c in changes if c[3]["source"] == source]
                    verb = "Added" if kind == "add_track" else "Moved"
                    self.history.log("location", source, f"Location: {label} ({verb.lower()} by Check & fix)",
                                     self.geo._tag_value(tag), mine)
                    tag_assets(self.immich, self.geo._tag_value(tag), [c[0] for c in mine])
            self.store.delete_suggestions(TABLE, done)
        return {"written": len(done), "skipped": len(asset_ids) - len(done)}

    def dismiss(self, finding_id: str, asset_ids: list[str] | None = None) -> int:
        """Leave these photos as they are (all of the finding without ids)."""
        rows = self.store.suggestions(TABLE)
        keys = [k for k, r in rows.items() if r["finding"] == finding_id
                and (asset_ids is None or r["asset_id"] in asset_ids)]
        self.store.delete_suggestions(TABLE, keys)
        return len(keys)
