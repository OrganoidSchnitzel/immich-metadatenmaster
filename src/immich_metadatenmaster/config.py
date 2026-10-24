"""Settings: defaults, overridden by `<DATA_DIR>/config.json` (written by the setup page), overridden
by environment variables (see .env.example)."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CONFIG_FILE = "config.json"

# Setting name -> environment variable.
ENV_NAMES = {
    "immich_url": "IMMICH_URL",
    "immich_api_key": "IMMICH_API_KEY",
    "immich_public_url": "IMMICH_PUBLIC_URL",
    "estimator": "AGE_ESTIMATOR",
    "device": "DEVICE",
    "mivolo_model": "MIVOLO_MODEL",
    "ollama_url": "OLLAMA_URL",
    "ollama_model": "OLLAMA_MODEL",
    "timezone": "TIMEZONE",
    "date_anchor": "DATE_ANCHOR",
    "tag_root": "TAG_ROOT",
    "write_description": "WRITE_DESCRIPTION",
    "remove_from_album": "REMOVE_FROM_ALBUM",
    "min_face_px": "MIN_FACE_PX",
    "use_original_for_small_faces": "USE_ORIGINAL_FOR_SMALL_FACES",
    "map_tile_url": "MAP_TILE_URL",
    "map_attribution": "MAP_ATTRIBUTION",
    "geocoder_url": "GEOCODER_URL",
    "place_names": "PLACE_NAMES",
    "ui_password": "UI_PASSWORD",
}
# Only ever taken from the environment: never shown, never written to config.json.
ENV_ONLY = {"ui_password"}

# Text settings for which "" is a real value; for the others "" means "use the default".
_EMPTY_ALLOWED = {"immich_url", "immich_api_key", "immich_public_url", "tag_root", "map_attribution", "geocoder_url"}

ESTIMATORS = ("mivolo", "ollama")
DATE_ANCHORS = ("middle", "start")


def _bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    immich_url: str = ""
    immich_api_key: str = ""
    # URL the browser uses to open photos in Immich (defaults to immich_url).
    immich_public_url: str = ""
    data_dir: Path = Path("data")

    # Age estimation backend: "mivolo" (local model) or "ollama" (vision LLM).
    estimator: str = "mivolo"
    device: str = "cpu"
    mivolo_model: str = "iitolstykh/mivolo_v2"
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5vl:7b"

    # Writing dates back to Immich.
    timezone: str = "UTC"
    # "middle": "1987" becomes 2 July 1987, "start": 1 January 1987.
    date_anchor: str = "middle"
    # Tag root for changed photos ("Metadatenmaster/Date/Manual", ".../Location/GPX"). Empty disables tagging.
    tag_root: str = "Metadatenmaster"
    write_description: bool = True
    # Default of the album page option: take photos out of the album once their date is written.
    remove_from_album: bool = False

    # Faces smaller than this (in the preview) are re-cropped from the original file.
    min_face_px: int = 64
    use_original_for_small_faces: bool = True

    # Background map of the location pages (any XYZ tile server).
    map_tile_url: str = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    map_attribution: str = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
    # Address search on the map (a Photon geocoder). Empty switches it off.
    geocoder_url: str = "https://photon.komoot.io"
    # Show place names next to location suggestions (asks the geocoder about their rounded positions).
    place_names: bool = True

    # Password for the web UI: from the UI_PASSWORD environment variable, or set on the settings page
    # (stored as a scrypt hash). Neither means no password.
    ui_password: str = ""
    ui_password_hash: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.immich_url and self.immich_api_key)

    @property
    def public_url(self) -> str:
        return (self.immich_public_url or self.immich_url).rstrip("/")

    @property
    def config_path(self) -> Path:
        return self.data_dir / CONFIG_FILE

    # --- loading ----------------------------------------------------------------------------

    @classmethod
    def load(cls, env: Mapping[str, str] | None = None) -> "Settings":
        """Defaults < config.json < environment. Raises ValueError for invalid values."""
        e = os.environ if env is None else env
        data_dir = Path(e.get("DATA_DIR") or cls.data_dir)
        values: dict[str, Any] = {}
        path = data_dir / CONFIG_FILE
        if path.exists():
            values.update(json.loads(path.read_text()))
            for name in ENV_ONLY:
                values.pop(name, None)
        for name in env_overrides(e):
            values[name] = e[ENV_NAMES[name]]
        if "timezone" not in values and e.get("TZ"):
            values["timezone"] = e["TZ"]
        settings = cls(data_dir=data_dir).with_values(values)
        settings.validate()
        return settings

    def with_values(self, values: Mapping[str, Any]) -> "Settings":
        """Copy with the given settings (strings from env/forms are converted), unknown keys ignored."""
        changes: dict[str, Any] = {}
        for f in fields(self):
            if f.name not in values or f.name == "data_dir":
                continue
            default, value = getattr(Settings, f.name), values[f.name]
            if isinstance(default, bool):
                value = _bool(value) if isinstance(value, str) else bool(value)
            elif isinstance(default, int):
                value = int(value)
            else:
                value = str(value if value is not None else "").strip()
                if not value and f.name not in _EMPTY_ALLOWED:
                    value = default
            changes[f.name] = value
        s = replace(self, **changes)
        return replace(
            s,
            immich_url=s.immich_url.rstrip("/"),
            ollama_url=s.ollama_url.rstrip("/"),
            estimator=s.estimator.lower(),
            date_anchor=s.date_anchor.lower(),
            tag_root=s.tag_root.strip("/"),
            timezone=s.timezone or "UTC",
        )

    def validate(self) -> None:
        if self.immich_url and not self.immich_url.startswith(("http://", "https://")):
            raise ValueError(f"The Immich URL must start with http:// or https:// (got '{self.immich_url}').")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"Unknown time zone '{self.timezone}' (use a name like Europe/Berlin).") from None
        if self.estimator not in ESTIMATORS:
            raise ValueError(f"Unknown age model '{self.estimator}' (use {' or '.join(ESTIMATORS)}).")
        if self.date_anchor not in DATE_ANCHORS:
            raise ValueError(f"Unknown date anchor '{self.date_anchor}' (use middle or start).")
        if self.min_face_px < 1:
            raise ValueError("The minimum face size must be positive.")
        if not self.map_tile_url.startswith(("http://", "https://", "/")):
            raise ValueError("The map tile URL must start with http:// or https://.")
        if self.geocoder_url and not self.geocoder_url.startswith(("http://", "https://")):
            raise ValueError("The address search URL must start with http:// or https://.")

    def save(self, exclude: set[str] = frozenset()) -> None:
        """Write the settings to config.json (owner-readable only: it holds the API key).

        `exclude` names settings that come from environment variables and must not be persisted.
        """
        data = {k: v for k, v in asdict(self).items() if k != "data_dir" and k not in exclude | ENV_ONLY}
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.config_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.chmod(0o600)
        tmp.replace(self.config_path)


def env_overrides(env: Mapping[str, str] | None = None) -> set[str]:
    """Settings fixed by environment variables (the setup page shows them read-only)."""
    e = os.environ if env is None else env
    return {name for name, var in ENV_NAMES.items() if e.get(var, "") != ""}
