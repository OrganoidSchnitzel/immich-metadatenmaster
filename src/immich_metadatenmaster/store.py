"""Local SQLite store: cached face ages, pending suggestions, GPX tracks and what was written to Immich."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS face_age (
    face_id TEXT NOT NULL,
    backend TEXT NOT NULL,
    age REAL NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (face_id, backend)
);
CREATE TABLE IF NOT EXISTS estimate (
    asset_id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dated (
    asset_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,          -- 'manual' or 'estimate'
    label TEXT NOT NULL,
    precision TEXT NOT NULL,       -- day | month | year | range | estimate
    start TEXT NOT NULL,
    end TEXT NOT NULL,
    written TEXT NOT NULL,         -- the date written to Immich
    written_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS name_date (
    asset_id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS version_date (
    asset_id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS geo_suggestion (
    asset_id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS correction (
    asset_id TEXT PRIMARY KEY,     -- "<finding id>|<asset id>": a suggested fix from Check & fix
    data TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS track (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    file_name TEXT NOT NULL,
    points TEXT NOT NULL,          -- JSON [[lat, lon, ele, unix time or null], ...]
    start TEXT,                    -- for tracks without times: the times entered by the user (UTC)
    end TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS change_batch (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,            -- 'date' or 'location'
    source TEXT NOT NULL,
    label TEXT NOT NULL,
    tag TEXT,                      -- tag given to the photos (removed again on undo)
    created_at TEXT NOT NULL,
    undone_at TEXT
);
CREATE TABLE IF NOT EXISTS change (
    batch_id INTEGER NOT NULL,
    asset_id TEXT NOT NULL,
    file_name TEXT NOT NULL,
    old TEXT NOT NULL,             -- JSON: the asset before the change
    new TEXT NOT NULL,             -- JSON: what was written
    PRIMARY KEY (batch_id, asset_id)
);
CREATE TABLE IF NOT EXISTS no_location (
    asset_id TEXT PRIMARY KEY,     -- marked by the user: a location makes no sense (screenshots, ...)
    file_name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS place_name (
    key TEXT PRIMARY KEY,          -- "lat,lon" rounded to 3 decimals, plus the language
    name TEXT NOT NULL,            -- "" if the geocoder knows no name there
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS library_scan (
    asset_id TEXT PRIMARY KEY,     -- every photo and video seen by the last library scan
    month TEXT NOT NULL,           -- "2019-07" from its date in Immich, or "unknown"
    flags INTEGER NOT NULL,        -- SCAN_* bits: what it lacks
    file_name TEXT NOT NULL,
    type TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS library_scan_month ON library_scan (month);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# What the library scan found missing (bits of library_scan.flags).
SCAN_NO_LOCATION = 1
SCAN_NO_DATE = 2      # no capture date in the file, and none in its name
SCAN_NAME_DATE = 4    # the file name holds a date Immich does not show
_MONTH = re.compile(r"^\d{4}-\d{2}$")


@dataclass(frozen=True)
class DatedRecord:
    asset_id: str
    source: str
    label: str
    precision: str
    start: date
    end: date
    written: date

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "label": self.label,
            "precision": self.precision,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "written": self.written.isoformat(),
        }


def _now() -> str:
    """UTC with its offset, so the browser can show it in local time (containers often run on UTC)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    # --- face ages --------------------------------------------------------------------------

    def face_ages(self, face_ids: list[str], backend: str) -> dict[str, float]:
        if not face_ids:
            return {}
        marks = ",".join("?" * len(face_ids))
        with self._lock:
            rows = self._db.execute(
                f"SELECT face_id, age FROM face_age WHERE backend = ? AND face_id IN ({marks})",
                [backend, *face_ids],
            ).fetchall()
        return dict(rows)

    def save_face_ages(self, ages: dict[str, float], backend: str) -> None:
        with self._lock, self._db:
            self._db.executemany(
                "INSERT OR REPLACE INTO face_age VALUES (?, ?, ?, ?)",
                [(fid, backend, age, _now()) for fid, age in ages.items()],
            )

    # --- estimates --------------------------------------------------------------------------

    def save_estimate(self, asset_id: str, data: dict[str, Any]) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO estimate VALUES (?, ?, ?)",
                             (asset_id, json.dumps(data), _now()))

    def estimates(self, asset_ids: list[str]) -> dict[str, dict[str, Any]]:
        return {k: json.loads(v) for k, v in self._select("estimate", "data", asset_ids).items()}

    def delete_estimates(self, asset_ids: list[str]) -> None:
        with self._lock, self._db:
            self._db.executemany("DELETE FROM estimate WHERE asset_id = ?", [(a,) for a in asset_ids])

    # --- dated ------------------------------------------------------------------------------

    def save_dated(self, rec: DatedRecord) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO dated VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (rec.asset_id, rec.source, rec.label, rec.precision, rec.start.isoformat(),
                 rec.end.isoformat(), rec.written.isoformat(), _now()),
            )

    def dated(self, asset_ids: list[str] | None = None) -> dict[str, DatedRecord]:
        cols = "asset_id, source, label, precision, start, end, written"
        with self._lock:
            if asset_ids is None:
                rows = self._db.execute(f"SELECT {cols} FROM dated").fetchall()
            else:
                rows = []
                for chunk in _chunks(asset_ids):
                    marks = ",".join("?" * len(chunk))
                    rows += self._db.execute(
                        f"SELECT {cols} FROM dated WHERE asset_id IN ({marks})", chunk).fetchall()
        return {
            r[0]: DatedRecord(r[0], r[1], r[2], r[3], date.fromisoformat(r[4]), date.fromisoformat(r[5]),
                              date.fromisoformat(r[6]))
            for r in rows
        }

    def delete_dated(self, asset_ids: list[str]) -> None:
        with self._lock, self._db:
            self._db.executemany("DELETE FROM dated WHERE asset_id = ?", [(a,) for a in asset_ids])

    # --- per-asset suggestions (name_date, geo_suggestion) ----------------------------------

    def save_suggestions(self, table: str, items: dict[str, dict[str, Any]], *, replace_all: bool = False) -> None:
        _check_table(table)
        with self._lock, self._db:
            if replace_all:
                self._db.execute(f"DELETE FROM {table}")
            self._db.executemany(f"INSERT OR REPLACE INTO {table} VALUES (?, ?, ?)",
                                 [(aid, json.dumps(data), _now()) for aid, data in items.items()])

    def suggestions(self, table: str, asset_ids: list[str] | None = None) -> dict[str, dict[str, Any]]:
        _check_table(table)
        if asset_ids is None:
            with self._lock:
                rows = self._db.execute(f"SELECT asset_id, data FROM {table} ORDER BY rowid").fetchall()
            return {k: json.loads(v) for k, v in rows}
        return {k: json.loads(v) for k, v in self._select(table, "data", asset_ids).items()}

    def delete_suggestions(self, table: str, asset_ids: list[str] | None = None) -> None:
        _check_table(table)
        with self._lock, self._db:
            if asset_ids is None:
                self._db.execute(f"DELETE FROM {table}")
            else:
                self._db.executemany(f"DELETE FROM {table} WHERE asset_id = ?", [(a,) for a in asset_ids])

    # --- tracks -----------------------------------------------------------------------------

    def save_track(self, track_id: str, name: str, file_name: str, points: list[list[Any]]) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT INTO track VALUES (?, ?, ?, ?, NULL, NULL, ?)",
                             (track_id, name, file_name, json.dumps(points), _now()))

    def find_track(self, points: list[list[Any]]) -> dict[str, str] | None:
        """A stored track with exactly these points (the same file uploaded again), if any."""
        with self._lock:
            row = self._db.execute("SELECT id, name FROM track WHERE points = ?", (json.dumps(points),)).fetchone()
        return {"id": row[0], "name": row[1]} if row else None

    def track(self, track_id: str) -> dict[str, Any] | None:
        return next((t for t in self.tracks(ids=[track_id])), None)

    def tracks(self, with_points: bool = True, ids: list[str] | None = None) -> list[dict[str, Any]]:
        cols = "id, name, file_name, start, end, created_at" + (", points" if with_points else "")
        where, args = "", []
        if ids is not None:
            where = f" WHERE id IN ({','.join('?' * len(ids))})" if ids else " WHERE 0"
            args = list(ids)
        with self._lock:
            rows = self._db.execute(f"SELECT {cols} FROM track{where} ORDER BY created_at, rowid", args).fetchall()
        out = []
        for r in rows:
            item = {"id": r[0], "name": r[1], "file_name": r[2], "start": r[3], "end": r[4], "created_at": r[5]}
            if with_points:
                item["points"] = json.loads(r[6])
            out.append(item)
        return out

    def set_track_times(self, track_id: str, start: str | None, end: str | None) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE track SET start = ?, end = ? WHERE id = ?", (start, end, track_id))

    def rename_track(self, track_id: str, name: str) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE track SET name = ? WHERE id = ?", (name, track_id))

    def delete_track(self, track_id: str) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM track WHERE id = ?", (track_id,))

    # --- change history ---------------------------------------------------------------------

    def add_changes(self, kind: str, source: str, label: str, tag: str | None,
                    changes: list[tuple[str, str, dict[str, Any], dict[str, Any]]]) -> int:
        """Log a batch of writes: (asset_id, file_name, old, new) each. Returns the batch id."""
        with self._lock, self._db:
            cur = self._db.execute("INSERT INTO change_batch (kind, source, label, tag, created_at) "
                                   "VALUES (?, ?, ?, ?, ?)", (kind, source, label, tag, _now()))
            batch_id = int(cur.lastrowid or 0)
            self._db.executemany("INSERT OR REPLACE INTO change VALUES (?, ?, ?, ?, ?)",
                                 [(batch_id, aid, name, json.dumps(old), json.dumps(new))
                                  for aid, name, old, new in changes])
            self._scan_written(kind, changes)
        return batch_id

    def change_batches(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT b.id, b.kind, b.source, b.label, b.tag, b.created_at, b.undone_at, COUNT(c.asset_id) "
                "FROM change_batch b LEFT JOIN change c ON c.batch_id = b.id "
                "GROUP BY b.id ORDER BY b.id DESC LIMIT ?", (limit,)).fetchall()
        return [{"id": r[0], "kind": r[1], "source": r[2], "label": r[3], "tag": r[4], "created_at": r[5],
                 "undone_at": r[6], "count": r[7]} for r in rows]

    def change_batch(self, batch_id: int) -> dict[str, Any] | None:
        with self._lock:
            r = self._db.execute("SELECT id, kind, source, label, tag, created_at, undone_at FROM change_batch "
                                 "WHERE id = ?", (batch_id,)).fetchone()
        if r is None:
            return None
        batch: dict[str, Any] = {"id": r[0], "kind": r[1], "source": r[2], "label": r[3], "tag": r[4],
                                 "created_at": r[5], "undone_at": r[6]}
        with self._lock:
            rows = self._db.execute("SELECT asset_id, file_name, old, new FROM change WHERE batch_id = ? "
                                    "ORDER BY rowid", (batch_id,)).fetchall()
        batch["changes"] = [{"asset_id": r[0], "file_name": r[1], "old": json.loads(r[2]), "new": json.loads(r[3])}
                            for r in rows]
        batch["count"] = len(rows)
        return batch

    def mark_undone(self, batch_id: int) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE change_batch SET undone_at = ? WHERE id = ?", (_now(), batch_id))

    # --- photos that need no location -------------------------------------------------------

    def mark_no_location(self, items: dict[str, str]) -> None:
        """Mark assets (id -> file name) as not needing a location."""
        with self._lock, self._db:
            self._db.executemany("INSERT OR REPLACE INTO no_location VALUES (?, ?, ?)",
                                 [(aid, name, _now()) for aid, name in items.items()])
            self._db.executemany("UPDATE library_scan SET flags = flags & ? WHERE asset_id = ?",
                                 [(~SCAN_NO_LOCATION, aid) for aid in items])

    def unmark_no_location(self, asset_ids: list[str]) -> None:
        with self._lock, self._db:
            self._db.executemany("DELETE FROM no_location WHERE asset_id = ?", [(a,) for a in asset_ids])

    def no_location(self) -> list[dict[str, str]]:
        with self._lock:
            rows = self._db.execute("SELECT asset_id, file_name, created_at FROM no_location "
                                    "ORDER BY created_at DESC, file_name").fetchall()
        return [{"id": r[0], "file_name": r[1], "created_at": r[2]} for r in rows]

    # --- place names (reverse geocoding cache) ----------------------------------------------

    def place_names(self, keys: list[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        with self._lock:
            for chunk in _chunks(keys):
                marks = ",".join("?" * len(chunk))
                out.update(self._db.execute(f"SELECT key, name FROM place_name WHERE key IN ({marks})",
                                            chunk).fetchall())
        return out

    def save_place_name(self, key: str, name: str) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO place_name VALUES (?, ?, ?)", (key, name, _now()))

    # --- library overview -------------------------------------------------------------------

    def replace_scan(self, rows: list[tuple[str, str, int, str, str]]) -> None:
        """Store a finished library scan: (asset_id, month, flags, file_name, type) per photo or video."""
        with self._lock, self._db:
            self._db.execute("DELETE FROM library_scan")
            self._db.executemany("INSERT OR REPLACE INTO library_scan VALUES (?, ?, ?, ?, ?)", rows)

    def scan_months(self) -> dict[str, dict[str, int]]:
        """Per month: photos and videos, and how many lack a location, a date or show a wrong one."""
        with self._lock:
            rows = self._db.execute(
                "SELECT month, COUNT(*), SUM((flags & ?) != 0), SUM((flags & ?) != 0), SUM((flags & ?) != 0) "
                "FROM library_scan GROUP BY month",
                (SCAN_NO_LOCATION, SCAN_NO_DATE, SCAN_NAME_DATE)).fetchall()
        return {r[0]: {"total": r[1], "no_location": r[2], "no_date": r[3], "name_date": r[4]} for r in rows}

    def scan_count(self, period: str) -> int:
        """Photos and videos the last scan saw in a month ("2019-07"), a year ("2019") or "unknown"."""
        where, args = _period(period)
        with self._lock:
            return int(self._db.execute(f"SELECT COUNT(*) FROM library_scan WHERE {where}", args).fetchone()[0])

    def scan_ids(self, flag: int, period: str) -> dict[str, list[str]]:
        """The photos of a period with the given SCAN_* bit, by month."""
        where, args = _period(period)
        with self._lock:
            rows = self._db.execute(f"SELECT month, asset_id FROM library_scan WHERE (flags & ?) != 0 AND {where}",
                                    [flag, *args]).fetchall()
        out: dict[str, list[str]] = {}
        for month, asset_id in rows:
            out.setdefault(month, []).append(asset_id)
        return out

    def update_scan(self, rows: dict[str, tuple[int, str]], gone: list[str] | None = None) -> None:
        """New flags and month of rescanned photos (id -> (flags, month)); forget deleted ones."""
        with self._lock, self._db:
            self._db.executemany("UPDATE library_scan SET flags = ?, month = ? WHERE asset_id = ?",
                                 [(flags, month, aid) for aid, (flags, month) in rows.items()])
            self._db.executemany("DELETE FROM library_scan WHERE asset_id = ?", [(a,) for a in gone or []])

    def location_written_ids(self) -> set[str]:
        """Photos whose location was written by this tool (and not undone)."""
        with self._lock:
            rows = self._db.execute("SELECT DISTINCT c.asset_id FROM change c JOIN change_batch b "
                                    "ON b.id = c.batch_id WHERE b.kind = 'location' AND b.undone_at IS NULL").fetchall()
        return {r[0] for r in rows}

    def date_written_ids(self) -> set[str]:
        """Photos whose date was written by this tool (and not undone): their file name no longer counts."""
        with self._lock:
            rows = self._db.execute("SELECT DISTINCT c.asset_id FROM change c JOIN change_batch b "
                                    "ON b.id = c.batch_id WHERE b.kind = 'date' AND b.undone_at IS NULL").fetchall()
        return {r[0] for r in rows}

    def _scan_written(self, kind: str, changes: list[tuple[str, str, dict[str, Any], dict[str, Any]]]) -> None:
        """Keep the library overview right after a write (called inside a transaction)."""
        if kind == "location":
            self._db.executemany("UPDATE library_scan SET flags = flags & ? WHERE asset_id = ?",
                                 [(~SCAN_NO_LOCATION, aid) for aid, *_ in changes])
        elif kind == "date":
            # The photo now has a capture date, chosen by the user; it may have moved to another month.
            self._db.executemany(
                "UPDATE library_scan SET flags = flags & ?, month = COALESCE(?, month) WHERE asset_id = ?",
                [(~(SCAN_NO_DATE | SCAN_NAME_DATE), _month_of_stamp(new.get("date")), aid)
                 for aid, _, _, new in changes])

    # --- small values -----------------------------------------------------------------------

    def get_value(self, key: str) -> Any:
        with self._lock:
            row = self._db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_value(self, key: str, value: Any) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, json.dumps(value)))

    def _select(self, table: str, column: str, asset_ids: list[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        with self._lock:
            for chunk in _chunks(asset_ids):
                marks = ",".join("?" * len(chunk))
                out.update(self._db.execute(
                    f"SELECT asset_id, {column} FROM {table} WHERE asset_id IN ({marks})", chunk).fetchall())
        return out


_SUGGESTION_TABLES = {"name_date", "geo_suggestion", "version_date", "correction"}


def _check_table(table: str) -> None:
    if table not in _SUGGESTION_TABLES:
        raise ValueError(f"unknown suggestion table {table}")


def _period(period: str) -> tuple[str, list[str]]:
    """SQL condition for a month ("2019-07"), a year ("2019") or "unknown"."""
    if period == "unknown" or _MONTH.match(period):
        return "month = ?", [period]
    if re.fullmatch(r"\d{4}", period):
        return "month LIKE ?", [f"{period}-%"]
    raise ValueError(f"Not a month or year: {period}")


def _month_of_stamp(stamp: str | None) -> str | None:
    """"1987-07-02T12:00:00.000+02:00" → "1987-07" (the wall clock time Immich shows)."""
    month = (stamp or "")[:7]
    return month if _MONTH.match(month) else None


def _chunks(items: list[str], size: int = 500):
    for i in range(0, len(items), size):
        yield items[i:i + size]
