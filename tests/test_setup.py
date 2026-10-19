"""First-run setup through the settings page."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from immich_metadatenmaster.config import Settings
from immich_metadatenmaster.immich import ImmichClient
from immich_metadatenmaster.service import AgeGuesser
from immich_metadatenmaster.store import Store
from immich_metadatenmaster.web.app import create_app


@pytest.fixture
def unconfigured(tmp_path, fake, estimator, monkeypatch):
    for var in ["IMMICH_URL", "IMMICH_API_KEY", "TIMEZONE", "TZ"]:
        monkeypatch.delenv(var, raising=False)
    transport = httpx.MockTransport(fake.handler)

    def client_for(s: Settings) -> ImmichClient:
        return ImmichClient(s.immich_url, s.immich_api_key, transport=transport)

    def build(s: Settings) -> AgeGuesser:
        return AgeGuesser(s, client_for(s), Store(s.data_dir / "db.sqlite"), estimator_factory=lambda _: estimator)

    app = create_app(settings=Settings(data_dir=tmp_path), build=build, make_client=client_for)
    with TestClient(app, headers={"X-Metadatenmaster": "1"}, follow_redirects=False) as c:
        yield c, app, tmp_path


def test_redirects_to_settings_until_configured(unconfigured):
    c, _, _ = unconfigured
    assert c.get("/").headers["location"] == "/settings"
    assert c.get("/people").status_code == 303
    assert c.get("/settings").status_code == 200
    assert c.get("/healthz").json()["configured"] is False
    assert c.get("/api/albums/x/assets").status_code == 503
    data = c.get("/api/settings").json()
    assert data["configured"] is False and data["has_api_key"] is False


def test_connection_test_reports_problems(unconfigured, fake):
    c, _, _ = unconfigured
    bad = c.post("/api/settings/test", json={"immich_url": "http://immich", "immich_api_key": "wrong"}).json()
    assert bad["ok"] is False
    fake.denied_paths.add("/people")
    r = c.post("/api/settings/test", json={"immich_url": "http://immich", "immich_api_key": "key"}).json()
    assert r["ok"] is False
    people = next(x for x in r["checks"] if "people" in x["label"])
    assert people["ok"] is False and "permission" in people["detail"]
    fake.denied_paths.clear()
    assert c.post("/api/settings/test", json={"immich_url": "http://immich", "immich_api_key": "key"}).json()["ok"]
    assert c.post("/api/settings/test", json={"immich_url": "nope", "immich_api_key": "key"}).status_code == 400


def test_save_settings_and_use_them(unconfigured, fake):
    c, app, tmp_path = unconfigured
    r = c.put("/api/settings", json={"immich_url": "http://immich/", "immich_api_key": "key",
                                     "timezone": "Europe/Berlin", "remove_from_album": True, "min_face_px": "50"})
    assert r.status_code == 200
    saved = json.loads((tmp_path / "config.json").read_text())
    assert saved["immich_url"] == "http://immich" and saved["immich_api_key"] == "key" and saved["min_face_px"] == 50
    assert c.get("/").status_code == 200
    data = c.get("/api/settings").json()
    assert data["has_api_key"] and "immich_api_key" not in data["values"]
    assert data["values"]["remove_from_album"] is True

    # Saving again without the key keeps it...
    assert c.put("/api/settings", json={"immich_url": "http://immich", "immich_api_key": "",
                                        "tag_root": "Scans"}).status_code == 200
    assert app.state.runtime.settings.immich_api_key == "key"
    assert app.state.runtime.settings.tag_root == "Scans"
    # ...but a new server address requires entering it again, so it is never sent elsewhere.
    r = c.put("/api/settings", json={"immich_url": "http://elsewhere", "immich_api_key": ""})
    assert r.status_code == 400 and "API key" in r.json()["detail"]


def test_invalid_settings_rejected(unconfigured):
    c, _, tmp_path = unconfigured
    r = c.put("/api/settings", json={"immich_url": "http://immich", "immich_api_key": "key", "timezone": "Mars"})
    assert r.status_code == 400 and not (tmp_path / "config.json").exists()


def test_env_values_are_locked(unconfigured, monkeypatch):
    c, app, tmp_path = unconfigured
    monkeypatch.setenv("TIMEZONE", "Europe/Berlin")
    assert "timezone" in c.get("/api/settings").json()["locked"]
    c.put("/api/settings", json={"immich_url": "http://immich", "immich_api_key": "key", "timezone": "Asia/Tokyo"})
    assert "timezone" not in json.loads((tmp_path / "config.json").read_text())
