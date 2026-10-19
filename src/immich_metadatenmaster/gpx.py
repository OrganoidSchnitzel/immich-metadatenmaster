"""GPX tracks (e.g. from Komoot, Strava, Garmin): parsing, and the position at a given time."""

from __future__ import annotations

import bisect
import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

MAX_GPX_BYTES = 50 * 1024 * 1024
_EARTH_RADIUS_M = 6_371_008.8
# A pause in the recording is only bridged by interpolation if the track stayed roughly in place.
_STATIONARY_M = 200.0


class GpxError(ValueError):
    pass


@dataclass(frozen=True)
class TrackPoint:
    lat: float
    lon: float
    ele: float | None = None
    time: datetime | None = None  # UTC


@dataclass(frozen=True)
class ParsedGpx:
    name: str
    points: list[TrackPoint]

    @property
    def has_times(self) -> bool:
        return sum(1 for p in self.points if p.time is not None) >= 2


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(el: ET.Element, name: str) -> str | None:
    for child in el:
        if _local(child.tag) == name:
            return (child.text or "").strip() or None
    return None


def parse_time(text: str) -> datetime | None:
    """GPX times are ISO 8601, normally UTC ("Z"); naive times are taken as UTC."""
    try:
        dt = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def parse_gpx(data: bytes, fallback_name: str = "Track") -> ParsedGpx:
    """All track points (trkpt) of a GPX 1.0/1.1 file; route points (rtept) if it has no track.

    Several tracks or segments are merged. Points with times are sorted by time.
    """
    if len(data) > MAX_GPX_BYTES:
        raise GpxError("The file is too large (more than 50 MB).")
    if b"<!ENTITY" in data[:4096] or b"<!DOCTYPE" in data[:4096]:
        raise GpxError("GPX files with a DOCTYPE are not supported.")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise GpxError(f"Not a valid GPX file: {exc}") from None
    if _local(root.tag) != "gpx":
        raise GpxError("Not a GPX file (the root element is not <gpx>).")

    name = None
    for el in root:
        if _local(el.tag) == "metadata":
            name = _child_text(el, "name")
        elif _local(el.tag) in {"trk", "rte"} and name is None:
            name = _child_text(el, "name")

    def collect(kind: str) -> list[TrackPoint]:
        points = []
        for el in root.iter():
            if _local(el.tag) != kind:
                continue
            try:
                lat, lon = float(el.get("lat", "")), float(el.get("lon", ""))
            except ValueError:
                continue
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                continue
            ele_text, time_text = _child_text(el, "ele"), _child_text(el, "time")
            try:
                ele = float(ele_text) if ele_text else None
            except ValueError:
                ele = None
            points.append(TrackPoint(lat, lon, ele, parse_time(time_text) if time_text else None))
        return points

    points = collect("trkpt") or collect("rtept")
    if len(points) < 2:
        raise GpxError("The file contains no track (fewer than two track points).")
    timed = [p for p in points if p.time is not None]
    if len(timed) >= 2:
        points = sorted(timed, key=lambda p: p.time)  # type: ignore[arg-type,return-value]
    else:
        points = [TrackPoint(p.lat, p.lon, p.ele) for p in points]
    return ParsedGpx(name or re.sub(r"\.gpx$", "", fallback_name, flags=re.I) or "Track", points)


def distance_m(a: TrackPoint, b: TrackPoint) -> float:
    """Great-circle distance in metres."""
    la1, la2 = math.radians(a.lat), math.radians(b.lat)
    dla, dlo = la2 - la1, math.radians(b.lon - a.lon)
    h = math.sin(dla / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(dlo / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def total_distance_m(points: list[TrackPoint]) -> float:
    return sum(distance_m(a, b) for a, b in zip(points, points[1:]))


def with_times(points: list[TrackPoint], start: datetime, end: datetime) -> list[TrackPoint]:
    """Give an untimed track (a planned route) times, assuming a constant pace from start to end."""
    if end <= start:
        raise GpxError("The end time must be after the start time.")
    steps = [0.0] + [distance_m(a, b) for a, b in zip(points, points[1:])]
    total = sum(steps) or 1.0
    out, travelled = [], 0.0
    for p, step in zip(points, steps):
        travelled += step
        out.append(TrackPoint(p.lat, p.lon, p.ele, start + (end - start) * (travelled / total)))
    return out


@dataclass(frozen=True)
class Position:
    lat: float
    lon: float
    ele: float | None
    # Seconds between the photo and the closest real track point (0 when interpolated between close points).
    time_diff_s: float
    method: str  # "interpolated" | "nearest"


class Track:
    """A timed track with fast lookup of the position at any instant."""

    def __init__(self, points: list[TrackPoint], *, assumed_times: bool = False) -> None:
        self.points = [p for p in points if p.time is not None]
        # Times spread over a planned route (see with_times): always interpolate, gaps mean nothing.
        self.assumed_times = assumed_times
        self._times = [p.time.timestamp() for p in self.points]  # type: ignore[union-attr]

    @property
    def start(self) -> datetime:
        return self.points[0].time  # type: ignore[return-value]

    @property
    def end(self) -> datetime:
        return self.points[-1].time  # type: ignore[return-value]

    def position_at(self, when: datetime, *, tolerance: timedelta, max_gap: timedelta) -> Position | None:
        """Where the track was at `when`.

        Between two points it interpolates linearly, unless they are more than `max_gap` apart in time
        and the track moved (the recording was paused while travelling); then, and before the start or
        after the end, the nearest point counts if it is at most `tolerance` away in time.
        """
        if not self.points:
            return None
        t = when.timestamp()
        i = bisect.bisect_left(self._times, t)
        tol = tolerance.total_seconds()
        if i < len(self._times) and self._times[i] == t:
            p = self.points[i]
            return Position(p.lat, p.lon, p.ele, 0.0, "interpolated")
        if i == 0 or i == len(self._times):
            p = self.points[0] if i == 0 else self.points[-1]
            diff = abs(t - self._times[0 if i == 0 else -1])
            return Position(p.lat, p.lon, p.ele, diff, "nearest") if diff <= tol else None

        a, b = self.points[i - 1], self.points[i]
        ta, tb = self._times[i - 1], self._times[i]
        to_nearest = min(t - ta, tb - t)
        if self.assumed_times or tb - ta <= max_gap.total_seconds() or distance_m(a, b) <= _STATIONARY_M:
            f = (t - ta) / (tb - ta)
            ele = a.ele + (b.ele - a.ele) * f if a.ele is not None and b.ele is not None else \
                (a.ele if a.ele is not None else b.ele)
            # Short steps are as good as exact; across a pause the uncertainty grows with the gap.
            diff = 0.0 if tb - ta <= 120 else to_nearest
            return Position(a.lat + (b.lat - a.lat) * f, a.lon + (b.lon - a.lon) * f, ele, diff, "interpolated")
        if to_nearest <= tol:
            p = a if t - ta <= tb - t else b
            return Position(p.lat, p.lon, p.ele, to_nearest, "nearest")
        return None

    def covers(self, when: datetime, tolerance: timedelta) -> bool:
        return self.start - tolerance <= when <= self.end + tolerance


def simplify(points: list[TrackPoint], max_points: int = 1500) -> list[list[float]]:
    """[[lat, lon], …] with at most about `max_points` entries, for drawing the track on a map."""
    step = max(1, math.ceil(len(points) / max_points))
    picked = points[::step]
    if picked and picked[-1] is not points[-1]:
        picked.append(points[-1])
    return [[round(p.lat, 6), round(p.lon, 6)] for p in picked]
