"""Several people: everyone logs in with their own Immich account and works on their own photos.

Logging in with email and password starts an Immich session; only its token is kept, never the
password. People who cannot log in with a password (e.g. OAuth only) paste an API key instead. Both
are stored server-side; the browser only gets a random session id in a cookie.

Each person's suggestions, tracks, history and calibration live in `users/<Immich user id>/` of the
data folder. The data of the single-user setup (directly in the data folder) belongs to the owner of
its API key, or else to the first Immich admin who logs in.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .immich import ImmichClient, ImmichError

SESSION_S = 30 * 86400
# How often a session is checked against Immich (a deleted user, a revoked token or API key).
RECHECK_S = 600
OWNER_FILE = "owner.json"
_USER_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS session (
    id TEXT PRIMARY KEY,           -- SHA-256 of the cookie value
    server TEXT NOT NULL,          -- the Immich URL it belongs to (a new URL logs everybody out)
    user_id TEXT NOT NULL,
    email TEXT NOT NULL,
    name TEXT NOT NULL,
    is_admin INTEGER NOT NULL,
    kind TEXT NOT NULL,            -- 'token' (an Immich login) or 'api_key'
    secret TEXT NOT NULL,
    expires REAL NOT NULL,
    checked REAL NOT NULL
);
"""


class LoginFailed(Exception):
    """Immich did not accept the email and password or the API key."""


@dataclass
class User:
    """A logged-in person (one browser session)."""

    session: str  # id of the session (not the cookie value)
    id: str
    email: str
    name: str
    is_admin: bool
    kind: str
    secret: str
    checked: float = 0.0

    @property
    def label(self) -> str:
        return self.name or self.email


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Sessions:
    """Who is logged in, in `sessions.sqlite` of the data folder (owner-readable only: it holds the
    Immich tokens)."""

    def __init__(self, data_dir: Path) -> None:
        path = data_dir / "sessions.sqlite"
        data_dir.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        path.chmod(0o600)
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.executescript(_SCHEMA)

    def create(self, server: str, info: dict[str, Any], kind: str, secret: str) -> str:
        """Start a session for this user of this Immich server; returns the value for the cookie."""
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock, self._db:
            self._db.execute("DELETE FROM session WHERE expires < ?", (now,))
            self._db.execute("INSERT INTO session VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (_hash(token), server, info["id"], info["email"], info["name"], int(info["is_admin"]),
                              kind, secret, now + SESSION_S, now))
        return token

    def get(self, server: str, token: str | None) -> User | None:
        if not token:
            return None
        with self._lock:
            row = self._db.execute("SELECT id, user_id, email, name, is_admin, kind, secret, checked FROM session "
                                   "WHERE id = ? AND server = ? AND expires > ?",
                                   (_hash(token), server, time.time())).fetchone()
        return User(row[0], row[1], row[2], row[3], bool(row[4]), row[5], row[6], row[7]) if row else None

    def update(self, user: User, info: dict[str, Any]) -> None:
        """Immich still knows the user: remember when, and their current name and role."""
        user.name, user.email, user.is_admin, user.checked = info["name"], info["email"], info["is_admin"], time.time()
        with self._lock, self._db:
            self._db.execute("UPDATE session SET name = ?, email = ?, is_admin = ?, checked = ? WHERE id = ?",
                             (user.name, user.email, int(user.is_admin), user.checked, user.session))

    def delete(self, session: str) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM session WHERE id = ?", (session,))

    def credentials(self, server: str, user_id: str) -> tuple[str, str] | None:
        """The credentials of the user's most recent session, if one is left."""
        with self._lock:
            row = self._db.execute("SELECT kind, secret FROM session WHERE server = ? AND user_id = ? AND expires > ? "
                                   "ORDER BY expires DESC LIMIT 1", (server, user_id, time.time())).fetchone()
        return (row[0], row[1]) if row else None

    def close(self) -> None:
        self._db.close()


def log_in(client: ImmichClient, *, email: str = "", password: str = "", api_key: str = ""
           ) -> tuple[dict[str, Any], str, str]:
    """Check the email and password (or the API key) with Immich: (the user, kind, secret).

    Raises LoginFailed if Immich does not accept them, ImmichError if Immich cannot be asked.
    """
    if api_key:
        client.set_credentials(api_key=api_key)
        try:
            return client.me(), "api_key", api_key
        except ImmichError as exc:
            if exc.status == 401:
                raise LoginFailed("Immich does not know this API key.") from None
            if exc.status == 403:
                raise LoginFailed("The API key needs the user.read permission.") from None
            raise
    try:
        info = client.login(email, password)
    except ImmichError as exc:
        if exc.status in (400, 401):
            raise LoginFailed("Wrong email or password.") from None
        raise
    return info, "token", info.pop("token")


def check(client: ImmichClient) -> dict[str, Any] | None:
    """The user, as Immich knows them now; None if Immich no longer accepts the credentials.
    Raises ImmichError if Immich cannot be asked."""
    try:
        return client.me()
    except ImmichError as exc:
        if exc.status == 401:
            return None
        raise


def owner(data_dir: Path) -> str | None:
    """The Immich user the data of the single-user setup belongs to, once known."""
    try:
        return json.loads((data_dir / OWNER_FILE).read_text())["user_id"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def has_single_user_data(data_dir: Path) -> bool:
    return any((data_dir / name).exists() for name in ("metadatenmaster.sqlite", "age-guesser.sqlite"))


def claim(data_dir: Path, info: dict[str, Any]) -> bool:
    """Hand the data of the single-user setup to this user (if nobody has it yet)."""
    if owner(data_dir) or not has_single_user_data(data_dir):
        return False
    tmp = data_dir / f"{OWNER_FILE}.tmp"
    tmp.write_text(json.dumps({"user_id": info["id"], "email": info.get("email", "")}))
    tmp.replace(data_dir / OWNER_FILE)
    return True


def user_dir(data_dir: Path, user_id: str) -> Path:
    """Where a user's data lives: the data folder itself for the owner of the single-user data."""
    if not _USER_ID.match(user_id):
        raise ValueError(f"Unexpected Immich user id '{user_id}'.")
    if owner(data_dir) == user_id:
        return data_dir
    return data_dir / "users" / user_id
