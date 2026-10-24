"""Versions of a photo (HDR merges, panoramas, edits, "(1)" copies) whose date differs from the original's.

HDR or panorama merges often lack the capture date, so Immich files them under the day they were exported.
This finds them by name (IMG_1234-HDR.dng → IMG_1234.CR2) and suggests the original's date, and its
location if the version has none. Nothing is written until the user accepts.
"""

from __future__ import annotations

import logging
import posixpath
from datetime import timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .config import Settings
from .geotag import sibling_key
from .history import History, iana_name, zone_of
from .immich import Asset, ImmichClient
from .service import tag_assets
from .store import Store

log = logging.getLogger(__name__)

Progress = Callable[[int, int], None]
TABLE = "version_date"
# Words in the names of versions, searched for in Immich (its file name search matches parts of names).
VERSION_WORDS = ("hdr", "pano", "edit", "bearbeitet", "enhanced", "denoise", "dxo", "export", "kopie", "copy", "(")
# A version showing a time this close to the original's is fine as it is.
SAME_TIME = timedelta(minutes=10)


def _noop(done: int, total: int) -> None:
    pass


def stem(file_name: str) -> str:
    return file_name.rsplit(".", 1)[0].strip().lower()


def is_version(file_name: str) -> bool:
    """IMG_1234-HDR.dng, IMG_1234_edited.jpg, IMG_1234 (1).jpg are versions; IMG_1234.CR2 is not."""
    return sibling_key(file_name) != stem(file_name)


def original_stamp(original: Asset, fallback_tz: str) -> tuple[str, str | None]:
    """The original's capture time as written to Immich (with its time zone if the file has one)."""
    zone = zone_of(original.time_zone)
    if zone is not None and original.taken_at is not None:
        return original.taken_at.astimezone(zone).isoformat(timespec="milliseconds"), iana_name(original.time_zone)
    assert original.local_datetime is not None
    return original.local_datetime.replace(tzinfo=ZoneInfo(fallback_tz)).isoformat(timespec="milliseconds"), \
        fallback_tz


class VersionDates:
    def __init__(self, settings: Settings, immich: ImmichClient, store: Store) -> None:
        self.settings = settings
        self.immich = immich
        self.store = store
        self.history = History(store, immich)

    def _versions(self, album_id: str | None) -> list[Asset]:
        if album_id:
            pool = self.immich.album_all_assets(album_id)
        else:
            found: dict[str, Asset] = {}
            for word in VERSION_WORDS:
                for a in self.immich.search_assets(originalFileName=word, withPeople=False):
                    found[a.id] = a
            pool = list(found.values())
        return sorted((a for a in pool if a.shown and is_version(a.file_name)), key=lambda a: a.file_name.lower())

    @staticmethod
    def _originals(version: Asset, candidates: list[Asset]) -> tuple[list[Asset], str]:
        """The originals of the shot (e.g. its RAW and JPEG), earliest first; empty and a reason if there is
        none or it is not clear which shot it is."""
        dated = sorted((o for o in candidates if o.local_datetime is not None and o.exif_datetime is not None),
                       key=lambda o: (o.local_datetime, o.file_name))  # type: ignore[arg-type,return-value]

        def one_shot(group: list[Asset]) -> bool:
            return all(abs(o.local_datetime - group[0].local_datetime) <= SAME_TIME for o in group)  # type: ignore[operator]

        if not dated:
            return [], "No original with a capture date found."
        if one_shot(dated):
            return dated, ""
        # Several photos of that name from different times (the camera's counter started again):
        # prefer the ones in the same folder as the version.
        folder = posixpath.dirname(version.original_path)
        near = [o for o in dated if posixpath.dirname(o.original_path) == folder]
        if near and one_shot(near):
            return near, ""
        return [], "Several photos with that name from different times; not sure which is the original."

    def find(self, album_id: str | None = None, progress: Progress = _noop) -> dict[str, Any]:
        """Store a suggestion for each version whose date differs from its original. Replaces earlier ones."""
        versions = self._versions(album_id)
        by_key: dict[str, list[Asset]] = {}
        results: dict[str, dict[str, Any]] = {}
        ambiguous = without_original = 0
        for i, v in enumerate(versions):
            key = sibling_key(v.file_name)
            if key not in by_key:
                by_key[key] = [o for o in self.immich.search_assets(originalFileName=key, withPeople=False)
                               if o.shown and stem(o.file_name) == key]
            originals, reason = self._originals(v, [o for o in by_key[key] if o.id != v.id])
            progress(i + 1, len(versions))
            if not originals:
                if "Several" in reason:
                    ambiguous += 1
                else:
                    without_original += 1
                continue
            original = originals[0]
            if v.local_datetime is not None and abs(v.local_datetime - original.local_datetime) <= SAME_TIME \
                    and v.exif_datetime is not None:  # type: ignore[operator]
                continue
            stamp, tz = original_stamp(original, self.settings.timezone)
            item: dict[str, Any] = {
                "file_name": v.file_name, "type": v.type,
                "current": v.local_datetime.isoformat() if v.local_datetime else None,
                "has_date": v.exif_datetime is not None,
                "original_id": original.id, "original_name": original.file_name,
                "new": original.local_datetime.isoformat() if original.local_datetime else None,
                "stamp": stamp, "time_zone": tz,
            }
            located = next((o for o in originals if o.has_location), None)
            if located is not None and not v.has_location:
                item["latitude"], item["longitude"] = located.latitude, located.longitude
                item["location_from"] = located.file_name
            results[v.id] = item
        self.store.save_suggestions(TABLE, results, replace_all=True)
        return {"versions": len(versions), "suggested": len(results), "ambiguous": ambiguous,
                "without_original": without_original}

    def suggestions(self) -> dict[str, dict[str, Any]]:
        return self.store.suggestions(TABLE)

    def discard(self, asset_ids: list[str]) -> None:
        self.store.delete_suggestions(TABLE, asset_ids)

    def apply(self, asset_ids: list[str], *, progress: Progress = _noop) -> dict[str, Any]:
        """Write the originals' dates (and locations) to the versions."""
        stored = self.store.suggestions(TABLE, asset_ids)
        todo = [aid for aid in asset_ids if aid in stored]
        old = self.history.snapshots(todo)
        dates: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = []
        places: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = []
        try:
            for i, aid in enumerate(todo):
                s = stored[aid]
                name, before = old.get(aid, ("", {}))
                name = name or s["file_name"]
                self.immich.set_date([aid], s["stamp"], s["time_zone"])
                dates.append((aid, name, before, {"date": s["stamp"], "time_zone": s["time_zone"],
                                                  "from": s["original_name"]}))
                if s.get("latitude") is not None:
                    self.immich.set_location([aid], s["latitude"], s["longitude"])
                    places.append((aid, name, before, {"latitude": s["latitude"], "longitude": s["longitude"],
                                                       "source": "sibling", "detail": s["original_name"]}))
                progress(i + 1, len(todo))
        finally:
            date_tag, place_tag = self._tag_value("Date/From original"), self._tag_value("Location/Same-name file")
            self.history.log("date", "original", "Dates copied from originals", date_tag, dates)
            self.history.log("location", "sibling", "Location: Same-name file", place_tag, places)
            tag_assets(self.immich, date_tag, [c[0] for c in dates])
            tag_assets(self.immich, place_tag, [c[0] for c in places])
            self.store.delete_suggestions(TABLE, [c[0] for c in dates])
        return {"written": len(dates), "locations": len(places), "skipped": len(asset_ids) - len(dates)}

    def _tag_value(self, name: str) -> str | None:
        return f"{self.settings.tag_root}/{name}" if self.settings.tag_root else None
