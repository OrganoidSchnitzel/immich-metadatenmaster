"""Log of everything this tool writes to Immich, with the previous values, so that changes can be undone."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .immich import Asset, ImmichClient
from .store import Store

log = logging.getLogger(__name__)

Progress = Callable[[int, int], None]
_UTC_OFFSET = re.compile(r"^UTC([+-])(\d{1,2})(?::?(\d{2}))?$")


def _noop(done: int, total: int) -> None:
    pass


def snapshot(asset: Asset) -> dict[str, Any]:
    """What an undo needs to restore: the capture time, its time zone, description and location."""
    return {
        "taken_at": asset.taken_at.isoformat() if asset.taken_at else None,
        "local": asset.local_datetime.isoformat() if asset.local_datetime else None,
        "time_zone": asset.time_zone,
        "description": asset.description,
        "latitude": asset.latitude,
        "longitude": asset.longitude,
    }


def zone_of(name: str | None) -> timezone | ZoneInfo | None:
    """An IANA zone ("Europe/Berlin") or Immich's fixed offsets ("UTC+2", "UTC-03:30")."""
    if not name:
        return None
    if m := _UTC_OFFSET.match(name.strip()):
        delta = timedelta(hours=int(m[2]), minutes=int(m[3] or 0))
        return timezone(-delta if m[1] == "-" else delta)
    if name.strip().upper() in {"UTC", "Z", "GMT"}:
        return timezone.utc
    try:
        return ZoneInfo(name.strip())
    except (ZoneInfoNotFoundError, ValueError):
        return None


def iana_name(name: str | None) -> str | None:
    """The name to send as Immich's timeZone field, or None if it is not an IANA zone."""
    return name if isinstance(zone_of(name), ZoneInfo) else None


def restore_stamp(old: dict[str, Any]) -> str | None:
    """The dateTimeOriginal to write to put a photo back at its old place in the timeline."""
    if not old.get("taken_at"):
        return None
    when = datetime.fromisoformat(old["taken_at"])
    zone = zone_of(old.get("time_zone")) or timezone.utc
    return when.astimezone(zone).isoformat(timespec="milliseconds")


class History:
    def __init__(self, store: Store, immich: ImmichClient) -> None:
        self.store = store
        self.immich = immich

    def snapshots(self, asset_ids: list[str]) -> dict[str, tuple[str, dict[str, Any]]]:
        """File name and current state of each asset (empty state if it cannot be read)."""
        out: dict[str, tuple[str, dict[str, Any]]] = {}
        for aid in asset_ids:
            try:
                a = self.immich.asset(aid)
                out[aid] = (a.file_name, snapshot(a))
            except Exception as exc:
                log.warning("Cannot read %s before changing it: %s", aid, exc)
                out[aid] = ("", {})
        return out

    def log(self, kind: str, source: str, label: str, tag: str | None,
            changes: list[tuple[str, str, dict[str, Any], dict[str, Any]]]) -> int | None:
        if not changes:
            return None
        return self.store.add_changes(kind, source, label, tag, changes)

    @staticmethod
    def undoable(batch: dict[str, Any]) -> bool:
        """Dates can always be put back; a location only if the photo had one before.

        Immich's API cannot remove a location again, only replace it.
        """
        if batch.get("undone_at"):
            return False
        if batch["kind"] == "date":
            return all(c["old"].get("taken_at") for c in batch["changes"])
        return all(c["old"].get("latitude") is not None for c in batch["changes"])

    def undo(self, batch_id: int, progress: Progress = _noop) -> dict[str, Any]:
        batch = self.store.change_batch(batch_id)
        if batch is None:
            raise ValueError("Unknown change")
        if batch.get("undone_at"):
            raise ValueError("This change was already undone.")
        changes = batch["changes"]
        restored, skipped = 0, 0
        for i, c in enumerate(changes):
            if self._restore(batch["kind"], c["asset_id"], c["old"], c["new"]):
                restored += 1
            else:
                skipped += 1
            progress(i + 1, len(changes))
        ids = [c["asset_id"] for c in changes]
        if batch["kind"] == "date":
            self.store.delete_dated(ids)
        if batch.get("tag"):
            try:
                self.immich.untag_assets(batch["tag"], ids)
            except Exception as exc:  # the old values are back; only the clean-up failed
                log.warning("Removing tag %s failed: %s", batch["tag"], exc)
        self.store.mark_undone(batch_id)
        return {"restored": restored, "skipped": skipped}

    def _restore(self, kind: str, aid: str, old: dict[str, Any], new: dict[str, Any]) -> bool:
        if kind == "date":
            stamp = restore_stamp(old)
            if stamp is None:
                return False
            self.immich.set_date([aid], stamp, iana_name(old.get("time_zone")))
            if "description" in new and old.get("description") is not None:
                self.immich.set_description(aid, old["description"])
            return True
        if old.get("latitude") is None or old.get("longitude") is None:
            return False
        self.immich.set_location([aid], old["latitude"], old["longitude"])
        return True
