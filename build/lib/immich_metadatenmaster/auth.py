"""Optional password for the web UI.

It is set on the settings page (stored as a scrypt hash in config.json) or with the UI_PASSWORD
environment variable. Logging in sets a signed cookie that is valid for 30 days; a new password logs
out every browser. Without a password the UI is open to everyone who can reach it.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import deque
from pathlib import Path

from .config import Settings

COOKIE = "mm_session"
SESSION_S = 30 * 86400
MIN_LENGTH = 8
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(stored: str, password: str) -> bool:
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        check = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                               dklen=len(digest) // 2)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(check.hex(), digest)


def password_state(settings: Settings) -> str:
    """"env" (UI_PASSWORD), "set" (on the settings page) or "none"."""
    return "env" if settings.ui_password else "set" if settings.ui_password_hash else "none"


class Auth:
    """Checks passwords and session cookies against the current settings."""

    def __init__(self, data_dir: Path) -> None:
        self._key_path = data_dir / "session.key"
        self._key: bytes | None = None

    @staticmethod
    def enabled(settings: Settings) -> bool:
        return password_state(settings) != "none"

    def _secret(self) -> bytes:
        """The key that signs session cookies, created on first use (owner-readable only)."""
        if self._key is None:
            try:
                self._key = bytes.fromhex(self._key_path.read_text().strip())
            except (OSError, ValueError):
                key = secrets.token_bytes(32)
                self._key_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._key_path.with_suffix(".tmp")
                tmp.write_text(key.hex())
                tmp.chmod(0o600)
                tmp.replace(self._key_path)
                self._key = key
        return self._key

    def check(self, settings: Settings, password: str) -> bool:
        if settings.ui_password:
            return hmac.compare_digest(password.encode(), settings.ui_password.encode())
        return bool(settings.ui_password_hash) and verify_password(settings.ui_password_hash, password)

    def _sign(self, settings: Settings, expires: int, nonce: str) -> str:
        # Signed together with the password (hash), so that a new password ends all sessions.
        password = f"env:{settings.ui_password}" if settings.ui_password else settings.ui_password_hash
        return hmac.new(self._secret(), f"{expires}.{nonce}.{password}".encode(), hashlib.sha256).hexdigest()

    def issue(self, settings: Settings) -> str:
        expires, nonce = int(time.time()) + SESSION_S, secrets.token_hex(8)
        return f"{expires}.{nonce}.{self._sign(settings, expires, nonce)}"

    def valid(self, settings: Settings, token: str | None) -> bool:
        try:
            expires_text, nonce, signature = (token or "").split(".")
            expires = int(expires_text)
        except ValueError:
            return False
        return expires > time.time() and hmac.compare_digest(signature, self._sign(settings, expires, nonce))


class LoginThrottle:
    """At most `limit` wrong passwords per `window_s`, for all clients together (behind a reverse proxy
    they all come from one address)."""

    def __init__(self, limit: int = 20, window_s: float = 600) -> None:
        self.limit, self.window_s = limit, window_s
        self._failures: deque[float] = deque()

    def blocked(self) -> bool:
        now = time.monotonic()
        while self._failures and self._failures[0] < now - self.window_s:
            self._failures.popleft()
        return len(self._failures) >= self.limit

    def failed(self) -> None:
        self._failures.append(time.monotonic())
