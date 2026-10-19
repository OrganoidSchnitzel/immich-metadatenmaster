"""Locations for photos without one: from GPX tracks, from nearby photos that have a location, and from
other versions of the same photo (e.g. the HDR merge of a RAW file). Nothing is written to Immich until
the user accepts the suggestions."""

from __future__ import annotations

import bisect
import logging
import re
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .config import Settings
from .gpx import ParsedGpx, Track, TrackPoint, parse_gpx, simplify, total_distance_m, with_times
from .history import History, iana_name, zone_of
from .immich import Asset, ImmichClient
from .service import tag_assets
from .store import Store

log = logging.getLogger(__name__)

Progress = Callable[[int, int], None]
TABLE = "geo_suggestion"
RUN_KEY = "geo_run"
MAX_ASSETS = 20_000
# Immich treats the clock time of photos without a time zone as UTC, so their stored instant can be
# off by up to this much; searches are widened accordingly and the exact time is computed here.
_TZ_SLACK = timedelta(hours=14)
_SIBLING_WINDOW = timedelta(hours=12)
_SUFFIX = re.compile(r"(?:[-_ ](?:hdr|pano|edit|edited|bearbeitet|enhanced(?:-nr|-sr)?|denoise|dxo|nr|lr|"
                     r"export|copy|kopie|\d{1,2}))+$", re.I)

SOURCES = {
    "sibling": ("Same-name file", "Location/Same-name file"),
    "gpx": ("GPX track", "Location/GPX"),
    "nearby": ("Nearby photo", "Location/Nearby photo"),
    "map": ("Placed on the map", "Location/Map"),
}


def _noop(done: int, total: int) -> None:
    pass


@dataclass
class GeoOptions:
    track_ids: list[str] | None = None  # None: all tracks
    use_tracks: bool = True
    # An explicit time range (UTC) to search in, in addition to the tracks' time spans.
    start: datetime | None = None
    end: datetime | None = None
    album_id: str | None = None
    # Time zone of camera clocks for photos that do not store one, and whether to trust a stored one.
    camera_tz: str = "UTC"
    use_photo_tz: bool = True
    # Added to the camera's time to get the real time (e.g. 3600 if the camera was an hour behind).
    offset_s: int = 0
    tolerance_min: float = 10
    max_gap_min: float = 15
    use_nearby: bool = True
    nearby_min: float = 10
    use_siblings: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {**{k: v for k, v in self.__dict__.items() if k not in {"start", "end"}},
                "start": self.start.isoformat() if self.start else None,
                "end": self.end.isoformat() if self.end else None}


@dataclass
class _Located:
    instants: list[float] = field(default_factory=list)
    assets: list[Asset] = field(default_factory=list)


def sibling_key(file_name: str) -> str:
    """IMG_1234.CR2, IMG_1234-HDR.dng and IMG_1234_edited.jpg all give "img_1234"."""
    stem = file_name.rsplit(".", 1)[0].strip()
    return _SUFFIX.sub("", stem).lower()


def photo_instant(asset: Asset, camera_tz: ZoneInfo, use_photo_tz: bool) -> datetime | None:
    """When the camera's clock says the photo was taken, as an instant."""
    if use_photo_tz and asset.time_zone and asset.taken_at and zone_of(asset.time_zone):
        return asset.taken_at
    if asset.local_datetime:
        return asset.local_datetime.replace(tzinfo=camera_tz).astimezone(timezone.utc)
    return asset.taken_at


def _confidence(source: str, time_diff_s: float) -> str:
    if source in {"sibling", "map"}:
        return "high"
    if source == "gpx":
        return "high" if time_diff_s <= 120 else "medium" if time_diff_s <= 600 else "low"
    return "medium" if time_diff_s <= 300 else "low"


def _merge(windows: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    out: list[tuple[datetime, datetime]] = []
    for start, end in sorted(windows):
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


class GeoTagger:
    def __init__(self, settings: Settings, immich: ImmichClient, store: Store) -> None:
        self.settings = settings
        self.immich = immich
        self.store = store
        self.history = History(store, immich)

    # --- tracks -----------------------------------------------------------------------------

    def add_track(self, file_name: str, data: bytes) -> dict[str, Any]:
        parsed: ParsedGpx = parse_gpx(data, file_name)
        track_id = uuid.uuid4().hex[:12]
        points = [[p.lat, p.lon, p.ele, p.time.timestamp() if p.time else None] for p in parsed.points]
        self.store.save_track(track_id, parsed.name, file_name, points)
        return self.track_summary(track_id)

    def _points(self, row: dict[str, Any]) -> list[TrackPoint]:
        utc = timezone.utc
        return [TrackPoint(lat, lon, ele, datetime.fromtimestamp(t, utc) if t is not None else None)
                for lat, lon, ele, t in row["points"]]

    def _timed_points(self, row: dict[str, Any]) -> list[TrackPoint] | None:
        """The track's points with times, or None for a route without times that has no times set yet."""
        points = self._points(row)
        if all(p.time is not None for p in points):
            return points
        if row["start"] and row["end"]:
            return with_times(points, datetime.fromisoformat(row["start"]), datetime.fromisoformat(row["end"]))
        return None

    def tracks(self) -> list[dict[str, Any]]:
        return [self._summary(row) for row in self.store.tracks()]

    def track_summary(self, track_id: str) -> dict[str, Any]:
        row = next((r for r in self.store.tracks() if r["id"] == track_id), None)
        if row is None:
            raise KeyError(track_id)
        return self._summary(row)

    def _summary(self, row: dict[str, Any]) -> dict[str, Any]:
        points = self._points(row)
        timed = self._timed_points(row)
        has_times = all(p.time is not None for p in points)
        return {
            "id": row["id"], "name": row["name"], "file_name": row["file_name"], "created_at": row["created_at"],
            "points": len(points), "distance_km": round(total_distance_m(points) / 1000, 1),
            "has_times": has_times, "needs_times": timed is None,
            "start": timed[0].time.isoformat() if timed else None,  # type: ignore[union-attr]
            "end": timed[-1].time.isoformat() if timed else None,  # type: ignore[union-attr]
            "line": simplify(points),
        }

    def set_track_times(self, track_id: str, start: datetime, end: datetime) -> dict[str, Any]:
        """Times for a route without times (e.g. a planned Komoot tour): start and end of the walk."""
        if end <= start:
            raise ValueError("The end time must be after the start time.")
        self.store.set_track_times(track_id, start.astimezone(timezone.utc).isoformat(),
                                   end.astimezone(timezone.utc).isoformat())
        return self.track_summary(track_id)

    def rename_track(self, track_id: str, name: str) -> dict[str, Any]:
        if not name.strip():
            raise ValueError("The name must not be empty.")
        self.store.rename_track(track_id, name.strip()[:200])
        return self.track_summary(track_id)

    def delete_track(self, track_id: str) -> None:
        self.store.delete_track(track_id)

    def _load_tracks(self, ids: list[str] | None) -> list[tuple[str, str, Track]]:
        out = []
        for row in self.store.tracks():
            if ids is not None and row["id"] not in ids:
                continue
            timed = self._timed_points(row)
            if timed:
                assumed = any(r[3] is None for r in row["points"])
                out.append((row["id"], row["name"], Track(timed, assumed_times=assumed)))
        return out

    # --- finding suggestions ----------------------------------------------------------------

    def find(self, opts: GeoOptions, progress: Progress = _noop) -> dict[str, Any]:
        """Search Immich for photos without a location in the tracks' time spans (and/or the given range)
        and store a suggestion for each. Replaces the previous suggestions."""
        cam_tz = ZoneInfo(opts.camera_tz)
        tol = timedelta(minutes=opts.tolerance_min)
        max_gap = timedelta(minutes=opts.max_gap_min)
        offset = timedelta(seconds=opts.offset_s)
        tracks = self._load_tracks(opts.track_ids) if opts.use_tracks else []

        scopes: list[tuple[datetime, datetime]] = [(t.start - tol, t.end + tol) for _, _, t in tracks]
        if opts.start and opts.end:
            if opts.end <= opts.start:
                raise ValueError("The end of the time range is before its start.")
            scopes.append((opts.start, opts.end))
        if not scopes:
            raise ValueError("Select at least one GPX track with times, or enter a time range.")
        scopes = _merge(scopes)
        slack = _TZ_SLACK + abs(offset) + tol + timedelta(minutes=opts.nearby_min)
        fetch = _merge([(a - slack, b + slack) for a, b in scopes])

        assets: dict[str, Asset] = {}
        for i, (a, b) in enumerate(fetch):
            for asset in self.immich.assets_taken_between(a, b, opts.album_id):
                assets[asset.id] = asset
            if len(assets) > MAX_ASSETS:
                raise ValueError(f"More than {MAX_ASSETS} photos in this time span. Select fewer tracks "
                                 "or a shorter range.")
            progress(i + 1, len(fetch))

        def in_scope(t: datetime) -> bool:
            return any(a <= t <= b for a, b in scopes)

        by_key: dict[str, list[Asset]] = {}
        with_location: list[tuple[float, Asset]] = []
        unlocated: list[tuple[Asset, datetime]] = []
        for a in assets.values():
            t = photo_instant(a, cam_tz, opts.use_photo_tz)
            if t is None:
                continue
            if a.has_location:
                by_key.setdefault(sibling_key(a.file_name), []).append(a)
                with_location.append((t.timestamp(), a))
            else:
                unlocated.append((a, t + offset))
        with_location.sort(key=lambda x: x[0])
        located = _Located([ts for ts, _ in with_location], [a for _, a in with_location])

        hint = self._shift_hint(unlocated, tracks, tol, max_gap) if tracks else None
        candidates = sorted(((a, t) for a, t in unlocated if in_scope(t)), key=lambda x: x[1])

        results: dict[str, dict[str, Any]] = {}
        sources: Counter[str] = Counter()
        steps = len(fetch) + len(candidates)
        for i, (a, t) in enumerate(candidates):
            item = self._base(a, t, opts, cam_tz)
            match = (self._sibling(a, by_key) if opts.use_siblings else None) \
                or self._on_tracks(t, tracks, tol, max_gap) \
                or (self._nearby(t, located, timedelta(minutes=opts.nearby_min)) if opts.use_nearby else None)
            if match:
                item.update(match, status="ok", confidence=_confidence(match["source"], match["time_diff_s"]))
                sources[match["source"]] += 1
            else:
                item.update(status="no_match", reason=self._reason(t, tracks))
            results[a.id] = item
            progress(len(fetch) + i + 1, steps)

        self.store.save_suggestions(TABLE, results, replace_all=True)
        run = {
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "options": opts.to_dict(),
            "track_ids": [tid for tid, _, _ in tracks],
            "searched": len(assets), "with_location": len(located.assets), "candidates": len(candidates),
            "suggested": sum(sources.values()), "sources": dict(sources), "hint": hint,
            "located": [[round(a.latitude, 6), round(a.longitude, 6)] for a in located.assets[:3000]
                        if a.latitude is not None and a.longitude is not None],
        }
        self.store.set_value(RUN_KEY, run)
        return {k: v for k, v in run.items() if k != "located"}

    def _base(self, a: Asset, t: datetime, opts: GeoOptions, cam_tz: ZoneInfo) -> dict[str, Any]:
        item: dict[str, Any] = {
            "file_name": a.file_name, "type": a.type, "camera": a.camera,
            "local": a.local_datetime.isoformat() if a.local_datetime else None,
            "time_zone": a.time_zone, "instant": t.isoformat(),
        }
        if opts.offset_s:
            # The corrected capture time, in the photo's own zone if it has one, else the camera zone.
            own = a.time_zone if opts.use_photo_tz and zone_of(a.time_zone) else None
            zone = zone_of(own) or cam_tz
            item["corrected"] = t.astimezone(zone).isoformat(timespec="milliseconds")
            item["corrected_tz"] = iana_name(own) if own else opts.camera_tz
        return item

    @staticmethod
    def _sibling(a: Asset, by_key: dict[str, list[Asset]]) -> dict[str, Any] | None:
        def apart(other: Asset) -> timedelta:
            if a.taken_at is None or other.taken_at is None:
                return timedelta(0)
            return abs(a.taken_at - other.taken_at)

        close = [o for o in by_key.get(sibling_key(a.file_name), []) if apart(o) <= _SIBLING_WINDOW]
        if not close:
            return None
        best = min(close, key=apart)
        return {"source": "sibling", "lat": best.latitude, "lon": best.longitude, "ele": None,
                "detail": best.file_name, "ref_id": best.id, "time_diff_s": 0.0, "method": "copied"}

    @staticmethod
    def _on_tracks(t: datetime, tracks: list[tuple[str, str, Track]], tol: timedelta,
                   max_gap: timedelta) -> dict[str, Any] | None:
        best = None
        for tid, name, track in tracks:
            if not track.covers(t, tol):
                continue
            pos = track.position_at(t, tolerance=tol, max_gap=max_gap)
            if pos and (best is None or pos.time_diff_s < best[2].time_diff_s):
                best = (tid, name, pos)
        if best is None:
            return None
        tid, name, pos = best
        return {"source": "gpx", "lat": pos.lat, "lon": pos.lon,
                "ele": round(pos.ele, 1) if pos.ele is not None else None,
                "detail": name, "ref_id": tid, "time_diff_s": round(pos.time_diff_s), "method": pos.method}

    @staticmethod
    def _nearby(t: datetime, located: _Located, window: timedelta) -> dict[str, Any] | None:
        if not located.instants:
            return None
        ts, w = t.timestamp(), window.total_seconds()
        i = bisect.bisect_left(located.instants, ts)
        before = i - 1 if i > 0 and ts - located.instants[i - 1] <= w else None
        after = i if i < len(located.instants) and located.instants[i] - ts <= w else None
        if before is None and after is None:
            return None
        if before is not None and after is not None and located.instants[after] > located.instants[before]:
            a, b = located.assets[before], located.assets[after]
            f = (ts - located.instants[before]) / (located.instants[after] - located.instants[before])
            diff = min(ts - located.instants[before], located.instants[after] - ts)
            lat = a.latitude + (b.latitude - a.latitude) * f  # type: ignore[operator]
            lon = a.longitude + (b.longitude - a.longitude) * f  # type: ignore[operator]
            nearest = a if ts - located.instants[before] <= located.instants[after] - ts else b
            return {"source": "nearby", "lat": lat, "lon": lon, "ele": None, "detail": nearest.file_name,
                    "ref_id": nearest.id, "time_diff_s": round(diff), "method": "interpolated"}
        k = before if before is not None else after
        ref = located.assets[k]  # type: ignore[index]
        return {"source": "nearby", "lat": ref.latitude, "lon": ref.longitude, "ele": None, "detail": ref.file_name,
                "ref_id": ref.id, "time_diff_s": round(abs(ts - located.instants[k])),  # type: ignore[index]
                "method": "nearest"}

    @staticmethod
    def _reason(t: datetime, tracks: list[tuple[str, str, Track]]) -> str:
        if not tracks:
            return "No photo with a location close enough in time."
        if any(track.start <= t <= track.end for _, _, track in tracks):
            return "Taken during a long pause in the recording."
        return "Outside the tracks' time span."

    @staticmethod
    def _shift_hint(unlocated: list[tuple[Asset, datetime]], tracks: list[tuple[str, str, Track]],
                    tol: timedelta, max_gap: timedelta) -> dict[str, Any] | None:
        """If many more photos fit the tracks with the clock shifted by whole hours, the camera's time zone
        (or summer time) is probably set wrong."""
        def fits(t: datetime) -> bool:
            return any(track.covers(t, tol) and track.position_at(t, tolerance=tol, max_gap=max_gap)
                       for _, _, track in tracks)

        counts = {h: sum(1 for _, t in unlocated if fits(t + timedelta(hours=h))) for h in range(-14, 15)}
        best = max(counts, key=lambda h: (counts[h], -abs(h)))
        if best != 0 and counts[best] >= 3 and counts[best] >= 1.5 * counts[0] + 1:
            return {"hours": best, "matches": counts[best], "current": counts[0]}
        return None

    # --- reviewing --------------------------------------------------------------------------

    def run(self) -> dict[str, Any] | None:
        return self.store.get_value(RUN_KEY)

    def suggestions(self) -> dict[str, dict[str, Any]]:
        return self.store.suggestions(TABLE)

    def place(self, asset_ids: list[str], lat: float, lon: float) -> int:
        """Put the given photos at a point chosen on the map (as a suggestion, still to be written)."""
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("Invalid coordinates.")
        stored = self.store.suggestions(TABLE, asset_ids)
        for item in stored.values():
            item.update(status="ok", source="map", lat=lat, lon=lon, ele=None, detail="", ref_id=None,
                        time_diff_s=0, method="manual", confidence="high")
            item.pop("reason", None)
        self.store.save_suggestions(TABLE, stored)
        return len(stored)

    def discard(self, asset_ids: list[str]) -> None:
        self.store.delete_suggestions(TABLE, asset_ids)

    def apply(self, asset_ids: list[str], *, fix_time: bool = False, progress: Progress = _noop) -> dict[str, Any]:
        """Write the accepted locations (and, if asked, the clock-corrected times) to Immich."""
        stored = self.store.suggestions(TABLE, asset_ids)
        todo = [aid for aid in asset_ids if (stored.get(aid) or {}).get("status") == "ok"]
        old = self.history.snapshots(todo)
        done: dict[str, list[str]] = {}
        times: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = []
        location_changes: dict[str, list[tuple[str, str, dict[str, Any], dict[str, Any]]]] = {}
        try:
            for i, aid in enumerate(todo):
                s = stored[aid]
                self.immich.set_location([aid], s["lat"], s["lon"])
                name, before = old.get(aid, ("", {}))
                name = name or s.get("file_name", "")
                location_changes.setdefault(s["source"], []).append(
                    (aid, name, before,
                     {"latitude": s["lat"], "longitude": s["lon"], "source": s["source"], "detail": s.get("detail")}))
                done.setdefault(s["source"], []).append(aid)
                if fix_time and s.get("corrected"):
                    tz = s.get("corrected_tz")
                    self.immich.set_date([aid], s["corrected"], tz)
                    times.append((aid, name, before, {"date": s["corrected"], "time_zone": tz}))
                progress(i + 1, len(todo))
        finally:
            for source, changes in location_changes.items():
                label, tag = SOURCES.get(source, (source, f"Location/{source}"))
                self.history.log("location", source, f"Location: {label}", self._tag_value(tag), changes)
            if times:
                offset = (self.run() or {}).get("options", {}).get("offset_s", 0)
                self.history.log("date", "clock", f"Clock corrected by {_format_offset(offset)}", None, times)
        for source, ids in done.items():
            tag_assets(self.immich, self._tag_value(SOURCES.get(source, ("", f"Location/{source}"))[1]), ids)
        self.store.delete_suggestions(TABLE, todo)
        return {"written": len(todo), "skipped": len(asset_ids) - len(todo), "times_fixed": len(times)}

    def _tag_value(self, name: str) -> str | None:
        return f"{self.settings.tag_root}/{name}" if self.settings.tag_root else None


def _format_offset(seconds: int) -> str:
    sign = "-" if seconds < 0 else "+"
    s = abs(int(seconds))
    return f"{sign}{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"

