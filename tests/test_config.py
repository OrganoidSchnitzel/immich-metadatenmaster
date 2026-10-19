import json
import stat
from pathlib import Path

import pytest

from immich_metadatenmaster.config import Settings, env_overrides


def test_defaults_without_anything(tmp_path):
    s = Settings.load({"DATA_DIR": str(tmp_path)})
    assert not s.configured and s.data_dir == tmp_path and s.timezone == "UTC"


def test_file_then_env_precedence(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps(
        {"immich_url": "http://file:2283", "immich_api_key": "filekey", "timezone": "Europe/Berlin",
         "remove_from_album": True, "unknown_key": 1}))
    s = Settings.load({"DATA_DIR": str(tmp_path), "IMMICH_URL": "http://env:2283/", "TZ": "America/New_York"})
    assert s.immich_url == "http://env:2283"          # env wins over the file
    assert s.immich_api_key == "filekey"
    assert s.timezone == "Europe/Berlin"               # TZ only fills in when nothing else is set
    assert s.remove_from_album is True and s.configured


def test_env_strings_are_converted(tmp_path):
    s = Settings.load({"DATA_DIR": str(tmp_path), "IMMICH_URL": "http://x", "IMMICH_API_KEY": "k",
                       "TAG_ROOT": "/Scans/Dated/", "WRITE_DESCRIPTION": "no", "MIN_FACE_PX": "80",
                       "REMOVE_FROM_ALBUM": "yes", "AGE_ESTIMATOR": "Ollama"})
    assert s.tag_root == "Scans/Dated" and s.write_description is False and s.min_face_px == 80
    assert s.remove_from_album is True and s.estimator == "ollama"
    assert env_overrides({"IMMICH_URL": "x", "DEVICE": ""}) == {"immich_url"}


def test_empty_form_values_fall_back_to_defaults():
    s = Settings().with_values({"device": "", "tag_root": "", "min_face_px": "64"})
    assert s.device == "cpu" and s.tag_root == ""


@pytest.mark.parametrize("env", [
    {"TIMEZONE": "Mars/Base"}, {"AGE_ESTIMATOR": "magic"}, {"DATE_ANCHOR": "end"}, {"IMMICH_URL": "immich:2283"},
])
def test_invalid(tmp_path, env):
    with pytest.raises(ValueError):
        Settings.load({"DATA_DIR": str(tmp_path), **env})


def test_save_is_private_and_skips_env_values(tmp_path):
    s = Settings(data_dir=tmp_path).with_values({"immich_url": "http://x", "immich_api_key": "secret"})
    s.save(exclude={"immich_url"})
    data = json.loads((tmp_path / "config.json").read_text())
    assert data["immich_api_key"] == "secret" and "immich_url" not in data and "data_dir" not in data
    assert stat.S_IMODE((tmp_path / "config.json").stat().st_mode) == 0o600
    assert Settings.load({"DATA_DIR": str(tmp_path), "IMMICH_URL": "http://x"}).immich_api_key == "secret"
