"""Locations for photos without one: from GPX tracks, from nearby photos that have a location, and from
other versions of the same photo (e.g. the HDR merge of a RAW file), or placed by the user on the map.
Nothing is written to Immich until the user accepts the suggestions."""

from __future__ import annotations

import bisect
import logging
import re
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tzfpy import get_tz

from .config import Settings
from .gpx import GpxError, ParsedGpx, Track, TrackPoint, distance_m, parse_gpx, simplify, total_distance_m, with_times
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
# Versions of the same shot (RAW, HDR merge, edits) carry the same camera time, give or take a bracket.
_SIBLING_WINDOW = timedelta(minutes=10)
# Faster than this between the photos before and after means a wrong clock or a flight: no interpolation.
MAX_SPEED_M_S = 70.0
_SCREENSHOT = re.compile(r"screenshot|screen ?shot|bildschirmfoto|bildschirmaufnahme|capture d.[ée]cran|"
                         r"schermafbeelding|captura de pantalla|scrnli|^svid_", re.I)
_GRAPHIC_EXT = (".png", ".gif", ".webp", ".bmp", ".svg")
_WORD_SUFFIX = re.compile(r"(?:[-_ ](?:hdr|pano|edit|edited|bearbeitet|enhanced(?:-nr|-sr)?|denoise|dxo|nr|lr|"
                          r"export|copy|kopie))+$", re.I)
_NUMBER_SUFFIX = re.compile(r"[-_ ]\d{1,2}$")
_COPY_SUFFIX = re.compile(r"\s*\(\d+\)$")  # "IMG_1234 (1).jpg", as browsers name a second download

SOURCES = {
    "sibling": ("Same-name file", "Location/Same-name file"),
    "gpx": ("GPX track", "Location/GPX"),
    "nearby": ("Nearby photo", "Location/Nearby photo"),
    "map": ("Placed on the map", "Location/Map"),
}


def _noop(done: int, total: int) -> None:
    pass


def place_zone(lat: float, lon: float) -> str | None:
    """The time zone at a position ("America/Denver"), from a map that comes with the tool."""
    try:
        name = get_tz(lon, lat)
        ZoneInfo(name)
    except (ValueError, ZoneInfoNotFoundError):
        return None
    return name or None


def lacks_zone(name: str | None) -> bool:
    """No time zone of its own: none, or UTC, which Immich assumes for camera clocks and videos."""
    zone = zone_of(name)
    return zone is None or zone == timezone.utc or (isinstance(zone, ZoneInfo) and zone.key in {"UTC", "Etc/UTC"})


def local_stamp(instant: str, zone: str) -> str:
    """A capture instant as the wall clock time at the place, e.g. "2022-08-25T13:45:44.000-06:00"."""
    return datetime.fromisoformat(instant).astimezone(ZoneInfo(zone)).isoformat(timespec="milliseconds")


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
    # The same per camera (by its name, e.g. "Canon EOS 80D"); cameras not listed use offset_s.
    camera_offsets: dict[str, int] = field(default_factory=dict)
    tolerance_min: float = 10
    max_gap_min: float = 15
    use_nearby: bool = True
    nearby_min: float = 10
    use_siblings: bool = True
    # Also list photos that already have a location (to correct a wrong one on the map).
    include_located: bool = False

    def offset_for(self, camera: str) -> int:
        return self.camera_offsets.get(camera, self.offset_s) if camera else self.offset_s

    def to_dict(self) -> dict[str, Any]:
        return {**{k: v for k, v in self.__dict__.items() if k not in {"start", "end"}},
                "start": self.start.isoformat() if self.start else None,
                "end": self.end.isoformat() if self.end else None}


@dataclass
class _Located:
    """Photos with a location, sorted by time (unix seconds)."""

    instants: list[float] = field(default_factory=list)
    assets: list[Asset] = field(default_factory=list)


def looks_like_screenshot(asset: Asset) -> bool:
    """Screenshots and graphics, which usually need no location."""
    name = asset.file_name.lower()
    return bool(_SCREENSHOT.search(name)) or (name.endswith(_GRAPHIC_EXT) and not asset.camera)


def sibling_key(file_name: str) -> str:
    """IMG_1234.CR2, IMG_1234-HDR.dng, IMG_1234_edited.jpg, IMG_1234-2.jpg and IMG_1234 (1).jpg all give
    "img_1234". A short number is only taken off if a longer one remains, so that "Holiday 1.jpg" and
    "Holiday 2.jpg" stay different photos."""
    stem = _COPY_SUFFIX.sub("", file_name.rsplit(".", 1)[0].strip())
    while True:
        shorter = _WORD_SUFFIX.sub("", stem)
        m = _NUMBER_SUFFIX.search(shorter)
        if m and re.search(r"\d{3,}", shorter[:m.start()]):
            shorter = shorter[:m.start()]
        if shorter == stem:
            return stem.lower()
        stem = shorter


def photo_instant(asset: Asset, camera_tz: ZoneInfo, use_photo_tz: bool) -> datetime | None:
    """When the camera's clock says the photo was taken, as an instant."""
    if use_photo_tz and asset.time_zone and asset.taken_at and zone_of(asset.time_zone):
        return asset.taken_at
    if asset.local_datetime:
        return asset.local_datetime.replace(tzinfo=camera_tz).astimezone(timezone.utc)
    return asset.taken_at


def _confidence(match: dict[str, Any]) -> str:
    source, diff = match["source"], match["time_diff_s"]
    if source in {"sibling", "map"}:
        return "high"
    if source == "gpx":
        return "high" if diff <= 120 else "medium" if diff <= 600 else "low"
    # Nearby photos: the same camera shares the clock; another camera's clock may be off.
    if match.get("same_camera"):
        return "high" if diff <= 120 else "medium" if diff <= 600 else "low"
    return "medium" if diff <= 300 else "low"


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
        # A recorded track only happened once; a planned route may be walked on several days.
        if parsed.has_times and (same := self.store.find_track(points)) is not None:
            raise GpxError(f"This track is already uploaded as “{same['name']}”.")
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
        row = self.store.track(track_id)
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
        for row in self.store.tracks(ids=ids):
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
        largest_offset = timedelta(seconds=max([abs(opts.offset_s), *map(abs, opts.camera_offsets.values())]))
        tracks = self._load_tracks(opts.track_ids) if opts.use_tracks else []

        scopes: list[tuple[datetime, datetime]] = [(t.start - tol, t.end + tol) for _, _, t in tracks]
        if opts.start and opts.end:
            if opts.end <= opts.start:
                raise ValueError("The end of the time range is before its start.")
            scopes.append((opts.start, opts.end))
        whole_album = not scopes and bool(opts.album_id)
        if not scopes and not whole_album:
            raise ValueError("Select a GPX track with times, enter a time range, or choose an album.")
        scopes = _merge(scopes)
        slack = _TZ_SLACK + largest_offset + tol + timedelta(minutes=opts.nearby_min)
        fetch = _merge([(a - slack, b + slack) for a, b in scopes])

        assets: dict[str, Asset] = {}
        if whole_album:
            assets = {a.id: a for a in self.immich.album_all_assets(opts.album_id)}  # type: ignore[arg-type]
            progress(1, 1)
        for i, (a, b) in enumerate(fetch):
            for asset in self.immich.assets_taken_between(a, b, opts.album_id):
                assets[asset.id] = asset
            if len(assets) > MAX_ASSETS:
                raise ValueError(f"More than {MAX_ASSETS} photos in this time span. Select fewer tracks "
                                 "or a shorter range.")
            progress(i + 1, len(fetch))
        if len(assets) > MAX_ASSETS:
            raise ValueError(f"More than {MAX_ASSETS} photos in this album.")
        # Live Photo videos are hidden in Immich and follow their photo; locked photos stay private.
        assets = {aid: a for aid, a in assets.items() if a.shown}

        def in_scope(t: datetime) -> bool:
            return whole_album or any(a <= t <= b for a, b in scopes)

        skip = {item["id"] for item in self.store.no_location()}
        by_key: dict[str, list[Asset]] = {}
        with_location: list[tuple[float, Asset]] = []
        # Unlocated photos with their corrected time and their camera's own clock time.
        unlocated: list[tuple[Asset, datetime, datetime]] = []
        for a in assets.values():
            t = photo_instant(a, cam_tz, opts.use_photo_tz)
            if t is None:
                continue
            if a.has_location:
                by_key.setdefault(sibling_key(a.file_name), []).append(a)
                with_location.append((t.timestamp(), a))
            elif a.id not in skip:
                unlocated.append((a, t + timedelta(seconds=opts.offset_for(a.camera)), t))
        with_location.sort(key=lambda x: x[0])
        located = _Located([ts for ts, _ in with_location], [a for _, a in with_location])
        by_camera: dict[str, _Located] = {}
        for ts, a in with_location:
            if a.camera:
                cam = by_camera.setdefault(a.camera, _Located())
                cam.instants.append(ts)
                cam.assets.append(a)

        hints = self._shift_hints(unlocated, tracks, tol, max_gap) if tracks else []
        candidates = sorted(((a, t, raw) for a, t, raw in unlocated if in_scope(t)), key=lambda x: x[1])
        window = timedelta(minutes=opts.nearby_min)

        results: dict[str, dict[str, Any]] = {}
        sources: Counter[str] = Counter()
        steps = len(fetch) + len(candidates)
        for i, (a, t, raw) in enumerate(candidates):
            item = self._base(a, t, opts, cam_tz)
            match = (self._sibling(a, by_key) if opts.use_siblings else None) \
                or self._on_tracks(t, tracks, tol, max_gap) \
                or (self._nearby(a, t, raw, located, by_camera, window) if opts.use_nearby else None)
            if match:
                item.update(match, status="ok", confidence=_confidence(match))
                sources[match["source"]] += 1
            else:
                item.update(status="no_match", reason=self._reason(t, tracks, opts.use_nearby))
            results[a.id] = item
            progress(len(fetch) + i + 1, steps)

        located_listed = 0
        if opts.include_located:
            for ts, a in with_location:
                t = datetime.fromtimestamp(ts, timezone.utc)
                if a.id in skip or not in_scope(t):
                    continue
                # No clock correction for these: their time was not used to find their location.
                item = self._base(a, t, opts, cam_tz, correct_time=False)
                item.update(status="located", current_lat=a.latitude, current_lon=a.longitude)
                results[a.id] = item
                located_listed += 1

        self.store.save_suggestions(TABLE, results, replace_all=True)
        cameras = self._camera_stats(unlocated, candidates, results, opts)
        run = {
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "options": opts.to_dict(),
            "track_ids": [tid for tid, _, _ in tracks],
            "searched": len(assets), "with_location": len(located.assets), "candidates": len(candidates),
            "suggested": sum(sources.values()), "sources": dict(sources), "hints": hints, "cameras": cameras,
            "whole_album": whole_album, "located_listed": located_listed,
            "no_location_needed": sum(1 for a in assets.values() if a.id in skip),
            "located": [[round(a.latitude, 6), round(a.longitude, 6)] for a in located.assets[:3000]
                        if a.latitude is not None and a.longitude is not None],
        }
        self.store.set_value(RUN_KEY, run)
        return {k: v for k, v in run.items() if k != "located"}

    def _base(self, a: Asset, t: datetime, opts: GeoOptions, cam_tz: ZoneInfo, *,
              correct_time: bool = True) -> dict[str, Any]:
        item: dict[str, Any] = {
            "file_name": a.file_name, "type": a.type, "camera": a.camera,
            "local": a.local_datetime.isoformat() if a.local_datetime else None,
            "time_zone": a.time_zone, "instant": t.isoformat(),
        }
        if looks_like_screenshot(a):
            item["screenshot"] = True
        offset = opts.offset_for(a.camera)
        if offset and correct_time:
            # The corrected capture time, in the photo's own zone if it has one, else the camera zone.
            own = a.time_zone if opts.use_photo_tz and zone_of(a.time_zone) else None
            zone = zone_of(own) or cam_tz
            item["corrected"] = t.astimezone(zone).isoformat(timespec="milliseconds")
            item["corrected_tz"] = iana_name(own) if own else opts.camera_tz
            item["offset_s"] = offset
        return item

    @staticmethod
    def _sibling(a: Asset, by_key: dict[str, list[Asset]]) -> dict[str, Any] | None:
        """Another version of the same shot that has a location: same name, same camera clock time.

        The camera clock time (not the instant) is compared: a RAW file without a time zone and its JPEG
        export with one show the same clock time, while Immich may store instants hours apart.
        """
        def apart(other: Asset) -> timedelta:
            if a.local_datetime is None or other.local_datetime is None:
                return timedelta.max
            return abs(a.local_datetime - other.local_datetime)

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

    @classmethod
    def _nearby(cls, a: Asset, t: datetime, raw: datetime, located: _Located, by_camera: dict[str, _Located],
                window: timedelta) -> dict[str, Any] | None:
        """Position from photos with a location taken shortly before/after.

        Photos of the same camera come first: they share its clock, so they are compared by the camera's
        own time (the clock offset does not matter). Other cameras (e.g. a phone) only count if the same
        camera has nothing close enough, and give a lower confidence.
        """
        same = by_camera.get(a.camera) if a.camera else None
        match = cls._neighbours(raw, same, window) if same else None
        if match:
            match["same_camera"] = True
            return match
        match = cls._neighbours(t, located, window)
        if match:
            match["same_camera"] = False
        return match

    @staticmethod
    def _neighbours(t: datetime, located: _Located, window: timedelta) -> dict[str, Any] | None:
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
            gap = located.instants[after] - located.instants[before]
            pa = TrackPoint(a.latitude, a.longitude)  # type: ignore[arg-type]
            pb = TrackPoint(b.latitude, b.longitude)  # type: ignore[arg-type]
            if distance_m(pa, pb) / gap <= MAX_SPEED_M_S:
                f = (ts - located.instants[before]) / gap
                diff = min(ts - located.instants[before], located.instants[after] - ts)
                nearest = a if ts - located.instants[before] <= located.instants[after] - ts else b
                return {"source": "nearby", "lat": pa.lat + (pb.lat - pa.lat) * f,
                        "lon": pa.lon + (pb.lon - pa.lon) * f, "ele": None, "detail": nearest.file_name,
                        "ref_id": nearest.id, "time_diff_s": round(diff),
                        "method": "interpolated"}
            # Too far apart for the time between them (a wrong clock or a flight): take the nearer one.
            before, after = (before, None) if ts - located.instants[before] <= located.instants[after] - ts \
                else (None, after)
        k = before if before is not None else after
        ref = located.assets[k]  # type: ignore[index]
        return {"source": "nearby", "lat": ref.latitude, "lon": ref.longitude, "ele": None, "detail": ref.file_name,
                "ref_id": ref.id, "time_diff_s": round(abs(ts - located.instants[k])),  # type: ignore[index]
                "method": "nearest"}

    @staticmethod
    def _reason(t: datetime, tracks: list[tuple[str, str, Track]], use_nearby: bool) -> str:
        if not tracks:
            return "No photo with a location close enough in time." if use_nearby else "No GPX track used."
        if any(track.start <= t <= track.end for _, _, track in tracks):
            return "Taken during a long pause in the recording."
        return "Outside the tracks' time span."

    @staticmethod
    def _shift_hints(unlocated: list[tuple[Asset, datetime, datetime]], tracks: list[tuple[str, str, Track]],
                     tol: timedelta, max_gap: timedelta) -> list[dict[str, Any]]:
        """Per camera: if many more of its photos fit the tracks with its clock shifted by whole hours, its
        time zone (or summer time) is probably set wrong."""
        def fits(t: datetime) -> bool:
            return any(track.covers(t, tol) and track.position_at(t, tolerance=tol, max_gap=max_gap)
                       for _, _, track in tracks)

        by_camera: dict[str, list[datetime]] = {}
        for a, t, _ in unlocated:
            by_camera.setdefault(a.camera, []).append(t)
        hints = []
        for camera, times in sorted(by_camera.items()):
            counts = {h: sum(1 for t in times if fits(t + timedelta(hours=h))) for h in range(-14, 15)}
            best = max(counts, key=lambda h: (counts[h], -abs(h)))
            if best != 0 and counts[best] >= 3 and counts[best] >= 1.5 * counts[0] + 1:
                hints.append({"camera": camera, "hours": best, "matches": counts[best], "current": counts[0]})
        return hints

    @staticmethod
    def _camera_stats(unlocated: list[tuple[Asset, datetime, datetime]],
                      candidates: list[tuple[Asset, datetime, datetime]],
                      results: dict[str, dict[str, Any]], opts: GeoOptions) -> list[dict[str, Any]]:
        """The cameras of the photos without a location near the searched time, with a sample of their photos
        (for working out a clock offset, also when the offset is so wrong that none is in the time span)."""
        stats: dict[str, dict[str, Any]] = {}
        for a, t, raw in sorted(unlocated, key=lambda x: x[2]):
            s = stats.setdefault(a.camera, {"camera": a.camera, "without_location": 0, "in_scope": 0, "suggested": 0,
                                            "offset_s": opts.offset_for(a.camera), "photos": []})
            s["without_location"] += 1
            if len(s["photos"]) < 100:
                s["photos"].append({"id": a.id, "file_name": a.file_name, "type": a.type,
                                    "local": a.local_datetime.isoformat() if a.local_datetime else None,
                                    "instant": raw.isoformat()})
        for a, _, _ in candidates:
            stats[a.camera]["in_scope"] += 1
            if results.get(a.id, {}).get("status") == "ok":
                stats[a.camera]["suggested"] += 1
        return sorted(stats.values(), key=lambda s: (-s["without_location"], s["camera"]))

    # --- clock offsets --------------------------------------------------------------------------

    def clock_candidates(self, asset_id: str, *, camera_tz: str, use_photo_tz: bool = True, hours: float = 6,
                         limit: int = 200) -> dict[str, Any]:
        """Photos of other cameras (e.g. a phone) taken around the time this photo's camera shows, to pick the
        one of the same moment: the difference of their times is the camera's clock offset."""
        tz = ZoneInfo(camera_tz)
        a = self.immich.asset(asset_id)
        t = photo_instant(a, tz, use_photo_tz)
        if t is None:
            raise ValueError("This photo has no date.")
        window = timedelta(hours=hours)
        others = []
        for o in self.immich.assets_taken_between(t - window - _TZ_SLACK, t + window + _TZ_SLACK):
            if o.id == a.id or not o.shown or o.camera == a.camera:
                continue
            ot = photo_instant(o, tz, use_photo_tz)
            if ot is None or abs(ot - t) > window:
                continue
            others.append({"id": o.id, "file_name": o.file_name, "camera": o.camera, "type": o.type,
                           "local": o.local_datetime.isoformat() if o.local_datetime else None,
                           "has_time_zone": bool(o.time_zone), "delta_s": round((ot - t).total_seconds())})
        # In time order, so the user can scan for the same moment however far off the clock is.
        others.sort(key=lambda x: x["delta_s"])
        photo = {"id": a.id, "file_name": a.file_name, "camera": a.camera,
                 "local": a.local_datetime.isoformat() if a.local_datetime else None}
        return {"photo": photo, "others": others[:limit]}

    # --- reviewing --------------------------------------------------------------------------

    def run(self) -> dict[str, Any] | None:
        return self.store.get_value(RUN_KEY)

    def suggestions(self) -> dict[str, dict[str, Any]]:
        return self.store.suggestions(TABLE)

    def place(self, asset_ids: list[str], lat: float, lon: float, label: str = "") -> int:
        """Put the given photos at a point chosen on the map (as a suggestion, still to be written)."""
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("Invalid coordinates.")
        if lat == 0 and lon == 0:
            raise ValueError("Immich treats 0, 0 as “no location”. Choose another point.")
        stored = self.store.suggestions(TABLE, asset_ids)
        for item in stored.values():
            item.update(status="ok", source="map", lat=lat, lon=lon, ele=None, detail=label[:200], ref_id=None,
                        time_diff_s=0, method="manual", confidence="high")
            item.pop("reason", None)
        self.store.save_suggestions(TABLE, stored)
        return len(stored)

    def discard(self, asset_ids: list[str]) -> None:
        self.store.delete_suggestions(TABLE, asset_ids)

    def mark_no_location(self, asset_ids: list[str]) -> int:
        """Remember that these photos need no location (screenshots, documents …); later searches skip them."""
        stored = self.store.suggestions(TABLE, asset_ids)
        self.store.mark_no_location({aid: stored.get(aid, {}).get("file_name", "") for aid in asset_ids})
        self.store.delete_suggestions(TABLE, asset_ids)
        return len(asset_ids)

    def unmark_no_location(self, asset_ids: list[str]) -> None:
        self.store.unmark_no_location(asset_ids)

    def no_location(self) -> list[dict[str, str]]:
        return self.store.no_location()

    def apply(self, asset_ids: list[str], *, fix_time: bool = False, local_time: bool = False,
              progress: Progress = _noop) -> dict[str, Any]:
        """Write the accepted locations to Immich. If asked, also the clock-corrected times, or, for photos
        without a time zone, the time at the place in its time zone (which includes the clock correction)."""
        stored = self.store.suggestions(TABLE, asset_ids)
        # (0, 0) is never suggested, and Immich would take it for "no location".
        todo = [aid for aid in asset_ids if (stored.get(aid) or {}).get("status") == "ok"
                and (stored[aid]["lat"], stored[aid]["lon"]) != (0, 0)]
        old = self.history.snapshots(todo)
        done: dict[str, list[str]] = {}
        times: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = []
        local_times: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = []
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
                zone = place_zone(s["lat"], s["lon"]) if local_time and lacks_zone(s.get("time_zone")) \
                    and s.get("instant") else None
                if zone:
                    stamp = local_stamp(s["instant"], zone)
                    self.immich.set_date([aid], stamp, zone)
                    local_times.append((aid, name, before, {"date": stamp, "time_zone": zone}))
                elif fix_time and s.get("corrected"):
                    tz = s.get("corrected_tz")
                    self.immich.set_date([aid], s["corrected"], tz)
                    times.append((aid, name, before, {"date": s["corrected"], "time_zone": tz}))
                progress(i + 1, len(todo))
        finally:
            # Also after an error part-way: log, tag and clear what was written, so nothing is written twice.
            for source, changes in location_changes.items():
                label, tag = SOURCES.get(source, (source, f"Location/{source}"))
                self.history.log("location", source, f"Location: {label}", self._tag_value(tag), changes)
            if times:
                offsets = {stored[aid].get("offset_s", 0) for aid, *_ in times}
                label = (f"Clock corrected by {_format_offset(offsets.pop())}" if len(offsets) == 1
                         else f"Clock corrected ({len(offsets)} different offsets)")
                self.history.log("date", "clock", label, None, times)
            self.history.log("date", "localtime", "Local time of the place", None, local_times)
            for source, ids in done.items():
                tag_assets(self.immich, self._tag_value(SOURCES.get(source, ("", f"Location/{source}"))[1]), ids)
            self.store.delete_suggestions(TABLE, [aid for ids in done.values() for aid in ids])
        written = sum(len(ids) for ids in done.values())
        return {"written": written, "skipped": len(asset_ids) - written, "times_fixed": len(times),
                "local_times": len(local_times)}

    def _tag_value(self, name: str) -> str | None:
        return f"{self.settings.tag_root}/{name}" if self.settings.tag_root else None


def _format_offset(seconds: int) -> str:
    sign = "-" if seconds < 0 else "+"
    s = abs(int(seconds))
    return f"{sign}{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"

