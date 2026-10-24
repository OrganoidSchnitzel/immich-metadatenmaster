"""Library overview: per month, how many photos and videos lack a location or a capture date, or have a
date in their file name that Immich does not show.

A scan reads the metadata of the whole library (no images). Writes made with this tool keep the numbers
up to date, so a new scan is only needed for changes made elsewhere. The photos without a date of a
month (or year) can be opened like an album: "scan:no-date:2019-07".
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .filename_dates import date_from_name, differs
from .geotag import looks_like_screenshot
from .immich import Asset, ImmichClient, ImmichError, iso_utc
from .store import SCAN_NAME_DATE, SCAN_NO_DATE, SCAN_NO_LOCATION, Store

Progress = Callable[[int, int], None]
KV_KEY = "library_scan"
TYPES = {"IMAGE", "VIDEO"}
# Lists that open like an album, by kind: (flag, title).
LISTS = {
    "no-date": (SCAN_NO_DATE, "No capture date"),
    "name-date": (SCAN_NAME_DATE, "Other date in the file name"),
}
LIST_PREFIX = "scan:"
_LIST_ID = re.compile(r"^scan:(no-date|name-date):(\d{4}(?:-(?:0[1-9]|1[0-2]))?|unknown)$")
_MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July", "August", "September",
                "October", "November", "December")


def _noop(done: int, total: int) -> None:
    pass


def month_of(asset: Asset) -> str:
    dt = asset.local_datetime
    return f"{dt.year:04d}-{dt.month:02d}" if dt else "unknown"


def flags_of(asset: Asset, *, location_needed: bool = True, date_written: bool = False) -> int:
    """What a photo lacks, as SCAN_* bits. Screenshots need no location. A date in the file name that
    agrees with Immich confirms the date; one the user overrode with this tool no longer counts."""
    flags = 0
    if not asset.has_location and location_needed and not looks_like_screenshot(asset):
        flags |= SCAN_NO_LOCATION
    named = date_from_name(asset.file_name)
    if named is not None:
        utc = asset.taken_at.replace(tzinfo=None) if asset.taken_at else None
        if not date_written and differs(named, asset.local_datetime, utc=utc):
            flags |= SCAN_NAME_DATE
    elif asset.exif_datetime is None:
        flags |= SCAN_NO_DATE
    return flags


def is_list(album_id: str | None) -> bool:
    return album_id is not None and album_id.startswith(LIST_PREFIX)


def parse_list(album_id: str) -> tuple[str, str]:
    """"scan:no-date:2019-07" → ("no-date", "2019-07")."""
    m = _LIST_ID.match(album_id)
    if not m:
        raise ValueError(f"Unknown list '{album_id}'.")
    return m[1], m[2]


def period_label(period: str) -> str:
    if period == "unknown":
        return "Unknown date"
    if len(period) == 4:
        return period
    return f"{_MONTH_NAMES[int(period[5:]) - 1]} {period[:4]}"


def list_title(album_id: str) -> str:
    kind, period = parse_list(album_id)
    return f"{LISTS[kind][1]} · {period_label(period)}"


def _period_range(month: str) -> tuple[datetime, datetime]:
    """Start and end (exclusive) of a month ("2019-07"), as UTC-aware wall clock times."""
    year, number = int(month[:4]), int(month[5:])
    start = datetime(year, number, 1)
    end = datetime(year + 1, 1, 1) if number == 12 else datetime(year, number + 1, 1)
    return start.replace(tzinfo=timezone.utc), end.replace(tzinfo=timezone.utc)


def _missing(exc: ImmichError) -> bool:
    """Immich answers 400 ("Not found or no asset.read access") or 404 for deleted assets."""
    return " 400:" in str(exc) or " 404:" in str(exc)


class LibraryScan:
    def __init__(self, immich: ImmichClient, store: Store) -> None:
        self.immich = immich
        self.store = store

    def _known(self) -> tuple[set[str], set[str]]:
        """Photos marked as needing no location, and photos dated with this tool."""
        return {item["id"] for item in self.store.no_location()}, self.store.date_written_ids()

    def scan(self, progress: Progress = _noop) -> dict[str, Any]:
        """Read the whole library. A cancelled scan keeps the previous results."""
        total = self.immich.count_assets() or 0
        no_location, written = self._known()
        rows: dict[str, tuple[str, str, int, str, str]] = {}
        seen = 0
        for page in self.immich.search_pages(size=1000):
            for a in page:
                # Hidden Live Photo videos follow their photo; locked photos stay private.
                if a.shown and a.type in TYPES:
                    flags = flags_of(a, location_needed=a.id not in no_location, date_written=a.id in written)
                    rows[a.id] = (a.id, month_of(a), flags, a.file_name, a.type)
            seen += len(page)
            progress(seen, max(total, seen))
        self.store.replace_scan(list(rows.values()))
        self.store.set_value(KV_KEY, {"scanned_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        return self.summary() or {}

    def summary(self) -> dict[str, Any] | None:
        """Totals and per-year, per-month numbers (newest first), or None before the first scan."""
        info = self.store.get_value(KV_KEY)
        if not info:
            return None
        keys = ("total", "no_location", "no_date", "name_date")
        months = self.store.scan_months()
        years: dict[str, dict[str, Any]] = {}
        for month in sorted(months, key=lambda m: (m != "unknown", m), reverse=True):
            year = month[:4] if month != "unknown" else month
            y = years.setdefault(year, {"year": year, "label": period_label(year), "months": [],
                                        **{k: 0 for k in keys}})
            for k in keys:
                y[k] += months[month][k]
            y["months"].append({"month": month, "label": period_label(month), **months[month]})
        totals = {k: sum(y[k] for y in years.values()) for k in keys}
        return {"scanned_at": info["scanned_at"], **totals, "years": list(years.values())}

    def list_assets(self, album_id: str) -> list[Asset]:
        """The photos of a list (e.g. no capture date in July 2019), read fresh from Immich and sorted by
        file name. Photos that no longer belong to it (dated since, deleted) are left out, and the
        overview is corrected."""
        kind, period = parse_list(album_id)
        flag = LISTS[kind][0]
        by_month = self.store.scan_ids(flag, period)
        ids = {aid for month_ids in by_month.values() for aid in month_ids}
        found: dict[str, Asset] = {}
        for month, month_ids in sorted(by_month.items()):
            # Reading the whole month is quicker than asking for each photo, unless only a few are needed.
            if month == "unknown" or len(month_ids) * 100 <= self.store.scan_count(month):
                continue
            start, end = _period_range(month)
            slack = timedelta(days=1)  # Immich filters by the UTC instant, up to 14 h from the local time
            wanted = set(month_ids)
            for page in self.immich.search_pages(size=1000, withPeople=True, takenAfter=iso_utc(start - slack),
                                                 takenBefore=iso_utc(end + slack)):
                found.update((a.id, a) for a in page if a.id in wanted)
        gone: list[str] = []
        for aid in sorted(ids - found.keys()):
            try:
                found[aid] = self.immich.asset(aid)
            except ImmichError as exc:
                if not _missing(exc):
                    raise
                gone.append(aid)
        no_location, written = self._known()
        updates: dict[str, tuple[int, str]] = {}
        out: list[Asset] = []
        for a in found.values():
            if not a.shown:
                gone.append(a.id)
                continue
            flags = flags_of(a, location_needed=a.id not in no_location, date_written=a.id in written)
            updates[a.id] = (flags, month_of(a))
            if flags & flag:
                out.append(a)
        self.store.update_scan(updates, gone)
        return sorted(out, key=lambda a: a.file_name.lower())

