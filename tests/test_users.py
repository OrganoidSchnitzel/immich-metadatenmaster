"""Several people, each logged in with their own Immich account."""

import json
import stat

import httpx
import pytest
from fastapi.testclient import TestClient

from immich_metadatenmaster import users
from immich_metadatenmaster.auth import COOKIE
from immich_metadatenmaster.config import Settings
from immich_metadatenmaster.immich import ImmichClient
from immich_metadatenmaster.service import AgeGuesser, open_store
from immich_metadatenmaster.web import app as web
from immich_metadatenmaster.web.app import create_app

HEADERS = {"X-Metadatenmaster": "1"}
OWNER = {"email": "owner@example.com", "password": "owner password"}
ANNA = {"email": "anna@example.com", "password": "anna password"}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.setattr(web, "FAILED_LOGIN_DELAY_S", 0)
    for var in ["IMMICH_URL", "IMMICH_API_KEY", "MULTI_USER", "UI_PASSWORD", "TIMEZONE", "TZ"]:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def make(tmp_path, fake, estimator):
    """An app with the given settings, talking to the fake Immich."""
    transport = httpx.MockTransport(fake.handler)
    built: list[AgeGuesser] = []

    clients: list[ImmichClient] = []

    def client_for(s: Settings) -> ImmichClient:
        clients.append(ImmichClient(s.immich_url, s.immich_api_key, transport=transport))
        return clients[-1]

    def build(s: Settings, client: ImmichClient | None = None) -> AgeGuesser:
        g = AgeGuesser(s, client or client_for(s), open_store(s.data_dir), estimator_factory=lambda _: estimator)
        built.append(g)
        return g

    def make_app(**values):
        s = Settings(immich_url="http://immich.test", immich_api_key="key", data_dir=tmp_path,
                     timezone="Europe/Berlin", min_face_px=40).with_values(values)
        app = create_app(settings=s, build=build, make_client=client_for)
        app.state.built, app.state.clients = built, clients
        return app

    fake.add_user("anna", ANNA["email"], "Anna", password=ANNA["password"], api_key="anna key")
    return make_app


def browser(app):
    return TestClient(app, headers=HEADERS, follow_redirects=False)


def wait(c, job):
    from test_web import wait as wait_web
    return wait_web(c, job)


def test_everyone_logs_in_with_immich(make, fake, tmp_path):
    app = make(multi_user=True)
    with browser(app) as c:
        assert c.get("/").headers["location"] == "/login?next=%2F"
        assert c.get("/api/library").json() == {"detail": "Log in first.", "login": True}
        page = c.get("/login").text
        assert 'name="email"' in page and 'name="api_key"' in page

        assert c.post("/api/login", json={**ANNA, "password": "wrong"}).json() == {"detail": "Wrong email or password."}
        assert c.post("/api/login", json={"email": ANNA["email"]}).status_code == 400
        r = c.post("/api/login", json=ANNA)
        assert r.status_code == 200 and r.json()["name"] == "Anna"
        assert "HttpOnly" in r.headers["set-cookie"]
        # The browser gets a random id; the Immich token stays on the server.
        token = next(iter(fake.tokens))
        assert token not in c.cookies[COOKIE] and fake.tokens[token] == "anna"
        assert stat.S_IMODE((tmp_path / "sessions.sqlite").stat().st_mode) == 0o600

        home = c.get("/").text
        assert "Anna" in home and 'id="logout" hidden' not in home
        assert c.get("/api/library").json() == {"scanned_at": None}
        # Anna's data lives in her own folder.
        assert (tmp_path / "users" / "anna" / "metadatenmaster.sqlite").exists()
        assert not (tmp_path / "metadatenmaster.sqlite").exists()

        # Logging out ends the Immich session as well.
        assert c.post("/api/logout").status_code == 200
        assert fake.tokens == {}
        assert c.get("/api/library").status_code == 401


def test_api_key_instead_of_a_password(make, fake):
    app = make(multi_user=True)
    with browser(app) as c:
        assert c.post("/api/login", json={"api_key": "nope"}).json()["detail"] == "Immich does not know this API key."
        fake.denied_paths.add("/users/me")
        assert c.post("/api/login", json={"api_key": "anna key"}).json()["detail"] == \
            "The API key needs the user.read permission."
        fake.denied_paths.clear()
        assert c.post("/api/login", json={"api_key": "anna key"}).status_code == 200
        assert c.get("/api/settings").json()["user"] == {"name": "Anna", "email": ANNA["email"], "is_admin": False,
                                                         "login": "API key"}
        c.post("/api/logout")
        assert "anna key" in fake.api_keys  # an API key is not deleted


def test_each_person_sees_their_own_photos_and_jobs(make, fake):
    fake.add_photo("IMG_0001.JPG", "2019-07-10T10:00:00")
    anna_photo = fake.add_photo("IMG_0002.JPG", "2019-07-11T10:00:00")
    fake.assets[anna_photo].owner = "anna"
    app = make(multi_user=True)
    with browser(app) as owner, browser(app) as anna:
        owner.post("/api/login", json=OWNER)
        anna.post("/api/login", json=ANNA)
        job = anna.post("/api/library/scan").json()
        assert owner.get(f"/api/jobs/{job['id']}").status_code == 404
        assert owner.post(f"/api/jobs/{job['id']}/cancel").status_code == 404
        assert wait(anna, job)["result"]["total"] == 1
        assert owner.get("/api/library").json() == {"scanned_at": None}
        assert wait(owner, owner.post("/api/library/scan").json())["result"]["total"] == 1
        assert anna.get("/api/jobs").json() == []

        # A job that reads the workspace in the background (the list of a library month).
        job = anna.post("/api/name-dates", json={"album_id": "scan:name-date:2019-07",
                                                 "asset_ids": [anna_photo]}).json()
        assert wait(anna, job)["status"] == "done"


def test_only_admins_change_settings(make):
    app = make(multi_user=True)
    with browser(app) as anna, browser(app) as owner:
        anna.post("/api/login", json=ANNA)
        data = anna.get("/api/settings").json()
        assert data["can_edit"] is False and data["values"]["multi_user"] is True
        assert anna.put("/api/settings", json={"tag_root": "X"}).status_code == 403
        assert anna.post("/api/settings/test", json={"immich_url": "http://immich.test"}).status_code == 403
        assert anna.put("/api/settings/password", json={"new": "something long"}).status_code == 403

        owner.post("/api/login", json=OWNER)
        assert owner.get("/api/settings").json()["can_edit"] is True
        assert owner.put("/api/settings", json={"tag_root": "X"}).json() == {"ok": True, "login": False}
        assert anna.get("/api/library").status_code == 200  # still logged in after the change
        # Tested with the admin's own login when no API key is given …
        r = owner.post("/api/settings/test", json={"immich_url": "http://immich.test", "immich_api_key": ""}).json()
        assert r["ok"] is True and len(r["checks"]) == 4
        # … but never sent to another server.
        r = owner.post("/api/settings/test", json={"immich_url": "http://other.test", "immich_api_key": ""}).json()
        assert [c["label"] for c in r["checks"]] == ["Reach Immich 3.2.0"]
        assert app.state.clients[-1].credentials == ("", "")


def test_another_immich_server_logs_everybody_out(make, fake):
    app = make(multi_user=True)
    with browser(app) as anna, browser(app) as owner:
        anna.post("/api/login", json=ANNA)
        owner.post("/api/login", json=OWNER)
        r = owner.put("/api/settings", json={"immich_url": "http://new.test", "immich_api_key": ""})
        assert r.json() == {"ok": True, "login": True}
        assert app.state.runtime.settings.immich_api_key == ""  # the key belonged to the old server
        assert anna.get("/api/library").status_code == 401
        assert owner.get("/api/library").status_code == 401


def test_single_user_data_goes_to_the_owner_of_the_api_key(make, fake, tmp_path):
    shot = fake.add_photo("Screenshot.png", "2020-01-01T10:00:00")
    single = make()
    with browser(single) as c:
        c.post("/api/geo/no-location", json={"asset_ids": [shot]})
    assert (tmp_path / "metadatenmaster.sqlite").exists()

    app = make(multi_user=True)
    with browser(app) as anna, browser(app) as owner:
        anna.post("/api/login", json=ANNA)  # not an admin, but the API key tells whose data it is
        assert json.loads((tmp_path / users.OWNER_FILE).read_text())["user_id"] == "owner"
        assert anna.get("/api/geo/no-location").json() == []
        owner.post("/api/login", json=OWNER)
        assert [x["id"] for x in owner.get("/api/geo/no-location").json()] == [shot]
        assert not (tmp_path / "users" / "owner").exists()


def test_without_an_api_key_the_first_admin_gets_the_data(make, fake, tmp_path):
    (tmp_path / "metadatenmaster.sqlite").write_bytes(b"")
    app = make(multi_user=True, immich_api_key="")
    with browser(app) as c:
        c.post("/api/login", json=ANNA)
        assert users.owner(tmp_path) is None
        c.post("/api/login", json=OWNER)
        assert users.owner(tmp_path) == "owner"


def test_turning_on_several_users_in_the_settings(make, fake, tmp_path):
    app = make()
    with browser(app) as c:
        c.post("/api/geo/no-location", json={"asset_ids": [fake.add_photo("Screenshot.png", "2020-01-01T10:00:00")]})
        assert c.get("/login").headers["location"] == "/"
        r = c.put("/api/settings", json={"immich_url": "http://immich.test", "multi_user": "true"})
        assert r.json() == {"ok": True, "login": True}
        assert users.owner(tmp_path) == "owner"
        assert c.get("/api/library").status_code == 401
        assert c.get("/login").status_code == 200
        c.post("/api/login", json=OWNER)
        assert len(c.get("/api/geo/no-location").json()) == 1

        # And off again: it needs the API key, and the owner finds their data where it was.
        assert c.put("/api/settings", json={"immich_url": "http://immich.test", "multi_user": "false"}).status_code \
            == 200
        assert len(c.get("/api/geo/no-location").json()) == 1


def test_first_setup_with_several_users(make, tmp_path):
    app = make(multi_user=True, immich_url="", immich_api_key="")
    with browser(app) as c:
        assert c.get("/").headers["location"] == "/settings"
        assert c.get("/settings").status_code == 200
        r = c.put("/api/settings", json={"immich_url": "http://immich.test", "multi_user": "true"})
        assert r.json() == {"ok": True, "login": True}
        assert c.get("/").headers["location"] == "/login?next=%2F"
        assert c.post("/api/login", json=ANNA).status_code == 200
        assert c.get("/").status_code == 200


def test_a_revoked_session_logs_out(make, fake, monkeypatch):
    app = make(multi_user=True)
    with browser(app) as c:
        c.post("/api/login", json=ANNA)
        fake.tokens.clear()  # e.g. the device removed in Immich's account settings
        assert c.get("/api/library").status_code == 200  # not checked again yet
        monkeypatch.setattr(users, "RECHECK_S", -1)
        assert c.get("/api/library").status_code == 401
        assert c.get("/").headers["location"] == "/login?next=%2F"


def test_one_model_for_everybody(make, fake, estimator):
    app = make(multi_user=True)
    with browser(app) as owner, browser(app) as anna:
        owner.post("/api/login", json=OWNER)
        anna.post("/api/login", json=ANNA)
        owner.get("/api/library")
        anna.get("/api/library")
        a, b = app.state.built
        assert a.store is not b.store and a.immich is not b.immich
        assert a.estimator is b.estimator
        assert a.calibration_path.parent != b.calibration_path.parent


def test_two_browsers_of_the_same_person(make, fake):
    app = make(multi_user=True)
    with browser(app) as first, browser(app) as second:
        first.post("/api/login", json=ANNA)
        second.post("/api/login", json=ANNA)
        assert first.get("/api/library").status_code == 200
        first.post("/api/logout")
        # The other browser carries on with its own Immich session.
        assert len(fake.tokens) == 1
        assert second.get("/api/library").status_code == 200
        assert len(app.state.built) == 1
