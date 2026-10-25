"""Importing recorded tours from Komoot (against a fake of its unofficial API)."""

import base64
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from immich_metadatenmaster.gpx import parse_gpx
from immich_metadatenmaster.komoot import Komoot, KomootError, KomootLoginFailed, to_gpx
from immich_metadatenmaster.web import app as web
from immich_metadatenmaster.web.app import create_app

START = "2024-07-01T08:00:00.000+02:00"


class FakeKomoot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.tours = {str(100 + i): {"id": 100 + i, "name": f"Hike {i}", "date": START, "distance": 5000.0 + i,
                                     "duration": 7200, "sport": "hike", "type": "tour_recorded"} for i in range(3)}
        self.token_valid = True

    def handler(self, request: httpx.Request) -> httpx.Response:
        user, _, password = base64.b64decode(request.headers["authorization"].split()[1]).decode().partition(":")
        path = request.url.path
        self.calls.append((path, user))
        if path == "/v006/account/email/me@example.com/":
            if (user, password) != ("me@example.com", "secret"):
                return httpx.Response(403, json={"error": "AccessDenied"})
            return httpx.Response(200, json={"username": "4711", "password": "tok", "user": {"displayname": "Me"}})
        if (user, password) != ("4711", "tok") or not self.token_valid:
            return httpx.Response(401, json={"error": "Unauthorized"})
        if path == "/v007/users/4711/tours/":
            q = request.url.params
            assert q["type"] == "tour_recorded" and q["sort_direction"] == "desc"
            page, limit = int(q["page"]), 2
            items = list(self.tours.values())[page * limit:(page + 1) * limit]
            return httpx.Response(200, json={"_embedded": {"tours": items},
                                             "page": {"number": page, "totalPages": 2, "size": limit}})
        if path.startswith("/v007/tours/"):
            tour = self.tours.get(path.rsplit("/", 1)[1])
            if tour is None:
                return httpx.Response(404, json={"error": "NotFound"})
            # Times in milliseconds since the start; each hike goes north from 47.0 (Hike 0 at 11.0 east).
            east = 11.0 + (tour["id"] - 100) * 0.1
            items = [{"lat": 47.0 + i * 0.001, "lng": east, "alt": 1000 + i, "t": i * 60_000} for i in range(30)]
            return httpx.Response(200, json={**tour, "_embedded": {"coordinates": {"items": items}}})
        return httpx.Response(404, json={"error": "NotFound"})


@pytest.fixture
def komoot():
    return FakeKomoot()


@pytest.fixture
def client(guesser, komoot, monkeypatch):
    monkeypatch.setattr(web, "FAILED_LOGIN_DELAY_S", 0)
    app = create_app(guesser, komoot_transport=httpx.MockTransport(komoot.handler))
    with TestClient(app, headers={"X-Metadatenmaster": "1"}) as c:
        yield c


def wait(c, job):
    for _ in range(200):
        job = c.get(f"/api/jobs/{job['id']}").json()
        if job["status"] in {"done", "failed", "cancelled"}:
            return job
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_points_get_their_times():
    data = to_gpx("A & B", [{"lat": 47.0, "lng": 11.0, "alt": 900, "t": 0},
                            {"lat": 47.1, "lng": 11.1, "t": 90_000}], datetime_of(START))
    parsed = parse_gpx(data)
    assert parsed.name == "A & B" and parsed.has_times
    assert [p.time.isoformat() for p in parsed.points] == ["2024-07-01T06:00:00+00:00", "2024-07-01T06:01:30+00:00"]
    # Times since 1970.
    absolute = parse_gpx(to_gpx("x", [{"lat": 47.0, "lng": 11.0, "t": 1719813600000},
                                      {"lat": 47.1, "lng": 11.0, "t": 1719813660000}], None))
    assert absolute.points[0].time.isoformat() == "2024-07-01T06:00:00+00:00"


def datetime_of(text):
    from datetime import datetime
    return datetime.fromisoformat(text)


def test_client(komoot):
    k = Komoot(transport=httpx.MockTransport(komoot.handler))
    with pytest.raises(KomootLoginFailed):
        k.log_in("me@example.com", "wrong")
    assert k.log_in("me@example.com", "secret") == {"user_id": "4711", "token": "tok", "name": "Me"}
    page = k.tours(0)
    assert page["pages"] == 2 and [t["id"] for t in page["tours"]] == ["100", "101"]
    name, data = k.gpx("100")
    assert name == "Hike 0" and len(parse_gpx(data).points) == 30
    with pytest.raises(KomootError):
        k.gpx("999")


def test_connect_list_and_import(client, komoot, guesser, fake):
    assert client.get("/api/komoot").json() == {"connected": False, "name": None}
    assert client.get("/api/komoot/tours").status_code == 400
    r = client.post("/api/komoot/connect", json={"email": "me@example.com", "password": "wrong"})
    assert (r.status_code, r.json()["detail"]) == (400, "Wrong email or password for Komoot.")
    assert client.post("/api/komoot/connect", json={"email": "me@example.com", "password": "secret"}).json() == \
        {"connected": True, "name": "Me"}
    # Only the token is kept, not the password.
    assert guesser.store.get_value("komoot") == {"user_id": "4711", "token": "tok", "name": "Me"}

    page = client.get("/api/komoot/tours").json()
    assert [(t["name"], t["track_id"]) for t in page["tours"]] == [("Hike 0", None), ("Hike 1", None)]
    assert client.get("/api/komoot/tours?page=1").json()["tours"][0]["name"] == "Hike 2"

    assert client.post("/api/komoot/import", json={"tour_ids": ["../x"]}).status_code == 400
    job = client.post("/api/komoot/import", json={"tour_ids": ["100", "101", "999"]}).json()
    assert job["label"] == "Importing tours from Komoot" and job["meta"] == {"page": "locations"}
    result = wait(client, job)["result"]
    assert result["imported"] == 2 and result["already"] == 0 and len(result["errors"]) == 1
    tracks = client.get("/api/tracks").json()
    assert sorted(t["name"] for t in tracks) == ["Hike 0", "Hike 1"]
    assert all(t["has_times"] and t["start"] == "2024-07-01T06:00:00+00:00" for t in tracks)

    # Imported tours are marked, and not imported twice.
    page = client.get("/api/komoot/tours").json()
    assert all(t["track_id"] for t in page["tours"])
    again = wait(client, client.post("/api/komoot/import", json={"tour_ids": ["100"]}).json())["result"]
    assert again == {"imported": 0, "already": 1, "errors": []}

    # A photo of the hike gets its position from the imported track (10 minutes in: 47.010).
    aid = fake.add_photo("IMG_0001.CR2", "2024-07-01T08:10:00")
    assert wait(client, client.post("/api/geo/find", json={}).json())["status"] == "done"
    item = next(i for i in client.get("/api/geo/suggestions").json()["items"] if i["id"] == aid)
    assert item["status"] == "ok" and abs(item["lat"] - 47.010) < 0.0005


def test_an_expired_token_disconnects(client, komoot, guesser):
    client.post("/api/komoot/connect", json={"email": "me@example.com", "password": "secret"})
    komoot.token_valid = False
    r = client.get("/api/komoot/tours")
    assert r.status_code == 400 and "Connect again" in r.json()["detail"]
    assert client.get("/api/komoot").json()["connected"] is False

    client.post("/api/komoot/connect", json={"email": "me@example.com", "password": "secret"})
    assert client.post("/api/komoot/disconnect").json() == {"connected": False, "name": None}
    assert guesser.store.get_value("komoot") is None


def test_komoot_unreachable(guesser, monkeypatch):
    def down(request):
        raise httpx.ConnectError("no route")

    app = create_app(guesser, komoot_transport=httpx.MockTransport(down))
    with TestClient(app, headers={"X-Metadatenmaster": "1"}) as c:
        r = c.post("/api/komoot/connect", json={"email": "me@example.com", "password": "secret"})
        assert r.status_code == 502 and "cannot be reached" in r.json()["detail"]
