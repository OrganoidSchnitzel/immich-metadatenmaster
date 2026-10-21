"""Log of everything this tool writes to Immich, with the previous values, so that changes can be undone."""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .immich import Asset, ImmichClient, ImmichError
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
    """Logs writes and undoes them.

    Immich's API rejects an empty location, but Immich treats exactly (0, 0) as "no GPS" and clears it
    when it processes the change (seen in Immich 3.x). Undoing a location written to a photo that had
    none therefore writes (0, 0) and reads the photo back; if (0, 0) stays, the written location is
    put back so that no photo is left in the Atlantic.
    """

    clear_poll_s = 0.5
    clear_attempts = 20

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
        if batch.get("undone_at"):
            return False
        if batch["kind"] == "date":
            return all(c["old"].get("taken_at") for c in batch["changes"])
        return True

    def undo(self, batch_id: int, progress: Progress = _noop) -> dict[str, Any]:
        """Put the previous values back. Photos changed in Immich since are left alone."""
        batch = self.store.change_batch(batch_id)
        if batch is None:
            raise ValueError("Unknown change")
        if batch.get("undone_at"):
            raise ValueError("This change was already undone.")
        changes = batch["changes"]
        counts = {"restored": 0, "skipped": 0, "changed": 0, "failed": 0}
        restored: list[str] = []
        for i, c in enumerate(changes):
            outcome = self._restore(batch["kind"], c["asset_id"], c["old"], c["new"])
            counts[outcome] += 1
            if outcome == "restored":
                restored.append(c["asset_id"])
            progress(i + 1, len(changes))
        if batch["kind"] == "date":
            self.store.delete_dated(restored)
        if batch.get("tag") and restored:
            try:
                self.immich.untag_assets(batch["tag"], restored)
            except Exception as exc:  # the old values are back; only the clean-up failed
                log.warning("Removing tag %s failed: %s", batch["tag"], exc)
        self.store.mark_undone(batch_id)
        return counts

    def _restore(self, kind: str, aid: str, old: dict[str, Any], new: dict[str, Any]) -> str:
        try:
            current = self.immich.asset(aid)
        except ImmichError as exc:  # e.g. deleted in Immich since
            log.warning("Cannot read %s for undo: %s", aid, exc)
            return "changed"
        if kind == "date":
            stamp = restore_stamp(old)
            if stamp is None:
                return "skipped"
            if not _same_instant(current.taken_at, new.get("date")):
                return "changed"
            self.immich.set_date([aid], stamp, iana_name(old.get("time_zone")))
            if "description" in new and old.get("description") is not None:
                self.immich.set_description(aid, old["description"])
            return "restored"
        if not (_same_degrees(current.latitude, new.get("latitude"))
                and _same_degrees(current.longitude, new.get("longitude"))):
            return "changed"
        if old.get("latitude") is not None and old.get("longitude") is not None:
            self.immich.set_location([aid], old["latitude"], old["longitude"])
            return "restored"
        return "restored" if self._clear_location(aid, new["latitude"], new["longitude"]) else "failed"

    def _clear_location(self, aid: str, lat: float, lon: float) -> bool:
        self.immich.set_location([aid], 0.0, 0.0)
        for _ in range(self.clear_attempts):
            if not self.immich.asset(aid).has_location:
                return True
            time.sleep(self.clear_poll_s)
        log.warning("Immich kept (0, 0) on %s; putting the written location back", aid)
        self.immich.set_location([aid], lat, lon)
        return False


def _same_degrees(a: float | None, b: float | None) -> bool:
    # Immich may round what it stores; 1e-5° is about a metre.
    return a is not None and b is not None and abs(a - b) < 1e-5


def _same_instant(current: datetime | None, written: str | None) -> bool:
    if current is None or not written:
        return False
    try:
        when = datetime.fromisoformat(written.replace("Z", "+00:00"))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return abs((current - when).total_seconds()) <= 60
