import json
import stat

import pytest
from fastapi.testclient import TestClient

from immich_metadatenmaster.auth import COOKIE, Auth, LoginThrottle, hash_password, verify_password
from immich_metadatenmaster.config import Settings
from immich_metadatenmaster.web import app as web
from immich_metadatenmaster.web.app import create_app, safe_next

HEADERS = {"X-Metadatenmaster": "1"}


@pytest.fixture(autouse=True)
def no_delay(monkeypatch):
    monkeypatch.setattr(web, "FAILED_LOGIN_DELAY_S", 0)


@pytest.fixture
def app(guesser):
    return create_app(guesser)


def browser(app):
    return TestClient(app, headers=HEADERS)


def test_hashing():
    stored = hash_password("correct horse")
    assert stored.startswith("scrypt$") and "correct" not in stored
    assert verify_password(stored, "correct horse")
    assert not verify_password(stored, "correct hors") and not verify_password("garbage", "x")
    assert hash_password("same") != hash_password("same")  # salted


def test_without_password_everything_is_open(app):
    with browser(app) as c:
        assert c.get("/").status_code == 200
        assert c.get("/api/library").status_code == 200
        assert c.get("/login", follow_redirects=False).headers["location"] == "/"
        assert c.get("/api/settings").json()["password"] == "none"
        assert 'id="logout" hidden' in c.get("/").text


def test_setting_a_password_locks_the_ui(app, settings):
    with browser(app) as owner:
        assert owner.put("/api/settings/password", json={"new": "short"}).status_code == 400
        r = owner.put("/api/settings/password", json={"new": "correct horse"})
        assert r.json() == {"ok": True, "password": "set"}
        cookie = r.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Secure" not in cookie
        # The owner's browser stays logged in.
        assert owner.get("/").status_code == 200 and 'id="logout" hidden' not in owner.get("/").text
        saved = json.loads(settings.config_path.read_text())
        assert verify_password(saved["ui_password_hash"], "correct horse") and "correct horse" not in json.dumps(saved)
        assert stat.S_IMODE((settings.data_dir / "session.key").stat().st_mode) == 0o600

    with browser(app) as stranger:
        r = stranger.get("/locations?from=2019-07-01T00:00", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login?next=%2Flocations%3Ffrom%3D2019-07-01T00%3A00"
        assert stranger.get("/api/library").json() == {"detail": "Log in first.", "login": True}
        assert stranger.get("/api/library").status_code == 401
        assert stranger.get("/thumb/x").status_code == 401
        assert stranger.post("/api/library/scan").status_code == 401
        assert stranger.get("/static/app.css").status_code == 200 and stranger.get("/healthz").status_code == 200
        page = stranger.get("/login?next=/locations").text
        assert 'data-next="/locations"' in page and 'href="/dates"' not in page  # no navigation

        r = stranger.post("/api/login", json={"password": "wrong"})
        assert (r.status_code, r.json()) == (401, {"detail": "Wrong password."})
        with TestClient(app) as no_header:  # another site cannot send the header
            assert no_header.post("/api/login", json={"password": "correct horse"}).status_code == 403
        assert stranger.post("/api/login", json={"password": "correct horse"}).status_code == 200
        assert stranger.get("/api/library").status_code == 200

        stranger.post("/api/logout")
        assert stranger.get("/api/library").status_code == 401


def test_a_new_password_logs_out_everybody_else(app):
    with browser(app) as owner, browser(app) as other:
        owner.put("/api/settings/password", json={"new": "first password"})
        other.post("/api/login", json={"password": "first password"})
        assert other.get("/api/library").status_code == 200

        wrong = owner.put("/api/settings/password", json={"current": "wrong", "new": "second password"})
        assert wrong.status_code == 400
        assert owner.put("/api/settings/password", json={"current": "first password",
                                                         "new": "second password"}).status_code == 200
        assert owner.get("/api/library").status_code == 200
        assert other.get("/api/library").status_code == 401

        # Removing it opens the UI again.
        assert owner.put("/api/settings/password", json={"current": "second password", "new": ""}).json()["password"] \
            == "none"
        assert other.get("/api/library").status_code == 200


def test_password_from_the_environment(guesser, settings, monkeypatch):
    monkeypatch.setenv("UI_PASSWORD", "from the env")
    s = settings.with_values({"ui_password": "from the env"})
    guesser.settings = s
    app = create_app(guesser, settings=s)
    with browser(app) as c:
        assert c.get("/api/settings").status_code == 401
        assert c.post("/api/login", json={"password": "from the env"}).status_code == 200
        data = c.get("/api/settings").json()
        assert data["password"] == "env" and "ui_password" not in data["values"] and "ui_password" in data["locked"]
        assert c.put("/api/settings/password", json={"new": "something else"}).status_code == 400
        # Saving the other settings never writes the password to config.json.
        assert c.put("/api/settings", json={"immich_url": "http://immich.test", "tag_root": "X"}).status_code == 200
        assert "from the env" not in settings.config_path.read_text()
        assert c.get("/api/library", headers={"X-Forwarded-Proto": "https"}).status_code == 200


def test_secure_cookie_behind_https(app):
    with browser(app) as c:
        r = c.put("/api/settings/password", json={"new": "correct horse"}, headers={"X-Forwarded-Proto": "https"})
        assert "Secure" in r.headers["set-cookie"]


def test_too_many_wrong_passwords(app, monkeypatch):
    with browser(app) as c:
        c.put("/api/settings/password", json={"new": "correct horse"})
    with browser(app) as c:
        for _ in range(20):
            assert c.post("/api/login", json={"password": "guess"}).status_code == 401
        assert c.post("/api/login", json={"password": "correct horse"}).status_code == 429


def test_sessions_expire_and_cannot_be_forged(tmp_path, monkeypatch):
    s = Settings(data_dir=tmp_path, ui_password_hash=hash_password("correct horse"))
    auth = Auth(tmp_path)
    token = auth.issue(s)
    assert auth.valid(s, token)
    expires, nonce, sig = token.split(".")
    assert not auth.valid(s, f"{int(expires) + 999}.{nonce}.{sig}")
    assert not auth.valid(s, None) and not auth.valid(s, "nonsense")
    assert not Auth(tmp_path / "other").valid(s, token)  # another key
    monkeypatch.setattr("immich_metadatenmaster.auth.time.time", lambda: int(expires) + 1)
    assert not auth.valid(s, token)


def test_throttle_window(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("immich_metadatenmaster.auth.time.monotonic", lambda: now[0])
    t = LoginThrottle(limit=2, window_s=60)
    t.failed()
    t.failed()
    assert t.blocked()
    now[0] += 61
    assert not t.blocked()


def test_safe_next():
    assert safe_next("/album/x?y=1") == "/album/x?y=1"
    for bad in ("https://evil.example", "//evil.example", "/\\evil.example", "", None, "/login?next=/x"):
        assert safe_next(bad) == "/"


def test_login_page_without_cookie_does_not_loop(app):
    with browser(app) as c:
        c.put("/api/settings/password", json={"new": "correct horse"})
    with browser(app) as c:
        assert c.get("/login").status_code == 200
        assert c.get("/settings", follow_redirects=False).headers["location"] == "/login?next=%2Fsettings"
        assert COOKIE not in c.cookies
