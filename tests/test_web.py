import time
from datetime import date

import pytest
from conftest import gpx
from fastapi.testclient import TestClient

from immich_metadatenmaster.web.app import create_app


@pytest.fixture
def client(guesser):
    with TestClient(create_app(guesser), headers={"X-Metadatenmaster": "1"}) as c:
        yield c


def wait(client, job):
    for _ in range(200):
        job = client.get(f"/api/jobs/{job['id']}").json()
        if job["status"] in {"done", "failed", "cancelled"}:
            return job
        time.sleep(0.02)
    raise AssertionError("job did not finish")


@pytest.fixture
def batch(fake):
    anna = fake.add_person("Anna", date(1980, 3, 1))
    oma = fake.add_person("Oma", None)
    ids = [
        fake.add_asset("b.jpg", date(1986, 8, 1), [anna], album="scans"),
        fake.add_asset("a.jpg", date(1986, 8, 1), [anna, oma], album="scans"),
        fake.add_asset("c.jpg", date(1975, 1, 1), [], album="scans"),
    ]
    return {"anna": anna, "oma": oma, "ids": ids}


def test_pages_render(client, batch):
    r = client.get("/")
    assert r.status_code == 200 and "not calibrated yet" in r.text and "0 tracks uploaded" in r.text
    r = client.get("/dates")
    assert r.status_code == 200 and "Album scans" in r.text and "not calibrated yet" in r.text
    assert client.get("/album/scans").status_code == 200
    assert client.get("/people").status_code == 200
    r = client.get("/locations")
    assert r.status_code == 200 and "tile.openstreetmap.org" in r.text and "Album scans" in r.text
    assert client.get("/history").status_code == 200
    assert client.get("/static/vendor/leaflet/leaflet.js").status_code == 200
    assert client.get("/album/missing").status_code == 404
    assert client.get(f"/thumb/{batch['ids'][0]}").headers["content-type"] == "image/png"


def test_album_assets_sorted_with_people(client, batch):
    data = client.get("/api/albums/scans/assets").json()
    assert [a["file_name"] for a in data["assets"]] == ["a.jpg", "b.jpg", "c.jpg"]
    people = {p["name"]: p for p in data["people"]}
    assert people["Anna"]["photos"] == 2 and people["Oma"]["birth_date"] is None


def test_parse_date(client):
    assert client.get("/api/parse-date", params={"text": "06.1987"}).json()["written"] == "1987-06-15"
    assert client.get("/api/parse-date", params={"text": "soon"}).json()["ok"] is False


def test_known_date_flow(client, batch, fake):
    job = client.post("/api/known-date", json={"asset_ids": batch["ids"][:2], "date": "1986"}).json()
    assert wait(client, job)["status"] == "done"
    assets = {a["id"]: a for a in client.get("/api/albums/scans/assets").json()["assets"]}
    assert assets[batch["ids"][0]]["dated"]["label"] == "1986"
    assert assets[batch["ids"][0]]["date"].startswith("1986-07-02")
    assert client.post("/api/known-date", json={"asset_ids": batch["ids"], "date": "nope"}).status_code == 400


def test_estimate_and_apply_flow(client, batch, fake):
    job = client.post("/api/estimate", json={"asset_ids": batch["ids"], "not_after": "1990"}).json()
    job = wait(client, job)
    assert job["status"] == "done"
    assets = {a["id"]: a for a in client.get("/api/albums/scans/assets").json()["assets"]}
    first = assets[batch["ids"][1]]["estimate"]
    assert first["status"] == "ok" and "Oma has no birthday" in first["notes"]
    assert assets[batch["ids"][2]]["estimate"]["status"] == "no_estimate"

    job = wait(client, client.post("/api/apply-estimates", json={"asset_ids": batch["ids"]}).json())
    assert job["result"] == {"written": 2, "skipped": 1}
    assets = {a["id"]: a for a in client.get("/api/albums/scans/assets").json()["assets"]}
    assert assets[batch["ids"][0]]["dated"]["source"] == "estimate"


def test_discard(client, batch):
    wait(client, client.post("/api/estimate", json={"asset_ids": batch["ids"][:1]}).json())
    client.post("/api/discard-estimates", json={"asset_ids": batch["ids"][:1]})
    assert client.get("/api/albums/scans/assets").json()["assets"][1]["estimate"] is None


def test_birthdays(client, batch, fake):
    people = client.get("/api/people").json()
    assert [p["name"] for p in people] == ["Anna", "Oma"]
    r = client.put(f"/api/people/{batch['oma']}/birthdate", json={"birth_date": "1930-02-01"})
    assert r.json()["birth_date"] == "1930-02-01" and fake.people[batch["oma"]]["birthDate"] == "1930-02-01"
    assert client.put(f"/api/people/{batch['oma']}/birthdate", json={"birth_date": "2999-01-01"}).status_code == 400


def test_failed_job_reports_error(client, batch, guesser):
    def boom(*a, **k):
        raise RuntimeError("model exploded")
    guesser.estimate = boom
    job = wait(client, client.post("/api/estimate", json={"asset_ids": batch["ids"]}).json())
    assert job["status"] == "failed" and "model exploded" in job["error"]


def test_write_and_remove_from_album(client, batch, fake):
    ids = batch["ids"]
    job = client.post("/api/known-date", json={"asset_ids": ids[:2], "date": "1986", "album_id": "scans",
                                               "remove_from_album": True}).json()
    result = wait(client, job)["result"]
    assert result["written"] == 2 and result["removed"] == 2
    assert fake.albums["scans"]["assets"] == [ids[2]]
    assert fake.assets[ids[0]].exif_date.startswith("1986")  # the photo itself is untouched otherwise


def test_estimates_removed_only_when_written(client, batch, fake):
    ids = batch["ids"]
    wait(client, client.post("/api/estimate", json={"asset_ids": ids}).json())
    result = wait(client, client.post("/api/apply-estimates", json={
        "asset_ids": ids, "album_id": "scans", "remove_from_album": True}).json())["result"]
    assert result == {"written": 2, "skipped": 1, "removed": 2}
    assert fake.albums["scans"]["assets"] == [ids[2]]  # the photo without a suggestion stays


def test_no_removal_by_default(client, batch, fake):
    job = client.post("/api/known-date", json={"asset_ids": batch["ids"], "date": "1986", "album_id": "scans"})
    assert "removed" not in wait(client, job.json())["result"]
    assert len(fake.albums["scans"]["assets"]) == 3


def test_state_changes_need_the_csrf_header(guesser, batch):
    with TestClient(create_app(guesser)) as c:
        r = c.post("/api/known-date", json={"asset_ids": batch["ids"], "date": "1986"})
        assert r.status_code == 403
        assert c.get("/api/albums/scans/assets").status_code == 200


def test_healthz(client):
    assert client.get("/healthz").json() == {"ok": True, "configured": True}


def test_name_dates_via_web(client, fake):
    wa = fake.add_asset("IMG-20190713-WA0001.jpg", date(2019, 7, 13), [], album="chat")
    scan = fake.add_asset("scan.jpg", date(1987, 1, 1), [fake.add_person("Anna", date(1980, 3, 1))], album="chat")
    job = wait(client, client.post("/api/name-dates", json={"album_id": "chat", "asset_ids": [wa, scan]}).json())
    assert job["result"] == {"checked": 2, "with_date": 1, "suggested": 1}
    wait(client, client.post("/api/estimate", json={"asset_ids": [scan]}).json())
    assets = {a["id"]: a for a in client.get("/api/albums/chat/assets").json()["assets"]}
    assert assets[wa]["name_date"]["label"] == "2019-07-13" and assets[scan]["name_date"] is None

    # "Write suggestions" writes the file-name date of one photo and the face estimate of the other.
    job = wait(client, client.post("/api/apply-estimates", json={"asset_ids": [wa, scan]}).json())
    assert job["result"] == {"written": 2, "skipped": 0}
    assert fake.assets[wa].exif_date.startswith("2019-07-13T12:00")
    assets = {a["id"]: a for a in client.get("/api/albums/chat/assets").json()["assets"]}
    assert assets[wa]["dated"]["source"] == "filename" and assets[scan]["dated"]["source"] == "estimate"


HIKE = gpx([(47.0 + i * 0.01, 11.0, f"2024-07-01T08:{i * 5:02d}:00Z") for i in range(12)], name="Hike")


def test_locations_flow(client, fake):
    r = client.post("/api/tracks", params={"file_name": "hike.gpx"}, content=HIKE)
    assert r.status_code == 200 and r.json()["name"] == "Hike"
    track_id = r.json()["id"]
    assert client.post("/api/tracks", params={"file_name": "x.gpx"}, content=b"nope").status_code == 400
    again = client.post("/api/tracks", params={"file_name": "hike (1).gpx"}, content=HIKE)
    assert again.status_code == 400 and "already uploaded as “Hike”" in again.json()["detail"]
    assert client.put(f"/api/tracks/{track_id}", json={"name": "Zugspitze"}).json()["name"] == "Zugspitze"

    a = fake.add_photo("IMG_0001.CR2", "2024-07-01T10:20:00")
    b = fake.add_photo("IMG_0002.CR2", "2024-07-01T10:25:00")
    job = wait(client, client.post("/api/geo/find", json={"camera_tz": "Europe/Berlin"}).json())
    assert job["status"] == "done" and job["result"]["suggested"] == 2
    data = client.get("/api/geo/suggestions").json()
    assert [i["id"] for i in data["items"]] == [a, b] and data["run"]["track_ids"] == [track_id]
    assert fake.location_updates == []

    assert client.post("/api/geo/place", json={"asset_ids": [b], "lat": 45.0, "lon": 9.0}).json() == {"placed": 1}
    job = wait(client, client.post("/api/geo/apply", json={"asset_ids": [a, b]}).json())
    assert job["result"] == {"written": 2, "skipped": 0, "times_fixed": 0}
    assert fake.assets[a].latitude == pytest.approx(47.04) and fake.assets[b].latitude == 45.0
    assert client.get("/api/geo/suggestions").json()["items"] == []

    history = client.get("/api/history").json()
    assert {h["source"] for h in history} == {"gpx", "map"} and all(h["undoable"] for h in history)

    assert client.delete(f"/api/tracks/{track_id}").status_code == 200
    assert client.get("/api/tracks").json() == []


def test_geo_find_validation(client):
    assert client.post("/api/geo/find", json={"camera_tz": "Mars/Base"}).status_code == 400
    assert client.post("/api/geo/find", json={"start": "2024-07-01T08:00"}).status_code == 400
    job = wait(client, client.post("/api/geo/find", json={}).json())  # no tracks, no range
    assert job["status"] == "failed" and "Select a GPX track" in job["error"]


def test_untimed_track_times_via_web(client):
    plan = gpx([(47.0, 11.0, None), (47.1, 11.0, None)], name="Plan")
    t = client.post("/api/tracks", params={"file_name": "plan.gpx"}, content=plan).json()
    assert t["needs_times"]
    # The same planned route may be walked again on another day.
    assert client.post("/api/tracks", params={"file_name": "plan.gpx"}, content=plan).status_code == 200
    r = client.put(f"/api/tracks/{t['id']}", json={"start": "2024-07-01T10:00", "end": "2024-07-01T09:00",
                                                    "timezone": "Europe/Berlin"})
    assert r.status_code == 400
    r = client.put(f"/api/tracks/{t['id']}", json={"start": "2024-07-01T10:00", "end": "2024-07-01T12:00",
                                                    "timezone": "Europe/Berlin"}).json()
    assert not r["needs_times"] and r["start"] == "2024-07-01T08:00:00+00:00"


def test_undo_via_web(client, batch, fake):
    wait(client, client.post("/api/known-date", json={"asset_ids": batch["ids"][:1], "date": "1986"}).json())
    history = client.get("/api/history").json()
    assert history[0]["undoable"] and history[0]["label"] == "Known date 1986"
    details = client.get(f"/api/history/{history[0]['id']}").json()
    assert details["changes"][0]["file_name"] == "b.jpg"
    job = wait(client, client.post(f"/api/history/{history[0]['id']}/undo").json())
    assert job["result"] == {"restored": 1, "skipped": 0, "changed": 0, "failed": 0}
    assert fake.assets[batch["ids"][0]].exif_date.startswith("2026-09-01T10:00")
    assert client.get("/api/history").json()[0]["undone_at"]
    assert client.post(f"/api/history/{history[0]['id']}/undo").status_code == 400


def test_geocode_endpoint(guesser):
    import httpx
    photon = httpx.MockTransport(lambda r: httpx.Response(200, json={"features": [
        {"geometry": {"coordinates": [10.9853, 47.4211]},
         "properties": {"name": "Zugspitze", "country": "Deutschland"}}]}))
    with TestClient(create_app(guesser, geocode_transport=photon), headers={"X-Metadatenmaster": "1"}) as c:
        r = c.get("/api/geocode", params={"q": "Zugspitze", "lang": "de"}).json()
        assert r["results"][0]["name"] == "Zugspitze" and r["results"][0]["lat"] == 47.4211
        assert c.get("/api/geocode", params={"q": "1.5, 2.5"}).json()["results"][0]["lon"] == 2.5
    down = httpx.MockTransport(lambda r: httpx.Response(500))
    with TestClient(create_app(guesser, geocode_transport=down), headers={"X-Metadatenmaster": "1"}) as c:
        assert c.get("/api/geocode", params={"q": "Zugspitze"}).status_code == 502


def test_no_location_and_album_scope_via_web(client, fake):
    shot = fake.add_photo("Screenshot_1.png", "2024-07-01T15:00:00", make="", model="", album="phone")
    photo = fake.add_photo("IMG_1.jpg", "2024-07-01T15:00:00", album="phone")
    job = wait(client, client.post("/api/geo/find", json={"use_tracks": False, "album_id": "phone"}).json())
    assert job["status"] == "done" and job["result"]["whole_album"]
    items = {i["id"]: i for i in client.get("/api/geo/suggestions").json()["items"]}
    assert items[shot]["screenshot"] and set(items) == {shot, photo}

    assert client.post("/api/geo/no-location", json={"asset_ids": [shot]}).json() == {"marked": 1}
    assert [x["id"] for x in client.get("/api/geo/no-location").json()] == [shot]
    assert client.post("/api/geo/place", json={"asset_ids": [photo], "lat": 0, "lon": 0}).status_code == 400
    r = client.post("/api/geo/place", json={"asset_ids": [photo], "lat": 48.1, "lon": 11.5, "label": "München"})
    assert r.json() == {"placed": 1}
    assert client.get("/api/geo/suggestions").json()["items"][0]["detail"] == "München"
    client.post("/api/geo/no-location/remove", json={"asset_ids": [shot]})
    assert client.get("/api/geo/no-location").json() == []


def test_local_time():
    from immich_metadatenmaster.web.app import local_time
    assert local_time("2026-10-21T11:58:00+00:00", "Europe/Berlin") == "2026-10-21 13:58"
    assert local_time("", "Europe/Berlin") == "" and local_time("nonsense", "Europe/Berlin") == "nonsense"


def test_jobs_can_be_listed_and_cancelled(client, guesser, batch):
    import threading
    gate = threading.Event()
    real = guesser.observations

    def slow(aid):
        gate.wait(5)
        return real(aid)

    guesser.observations = slow
    job = client.post("/api/estimate", json={"asset_ids": batch["ids"], "album_id": "scans"}).json()
    assert job["label"] == "Analysing faces" and job["meta"] == {"album_id": "scans"} and job["cancellable"]
    assert [j["id"] for j in client.get("/api/jobs").json()] == [job["id"]]
    assert client.post(f"/api/jobs/{job['id']}/cancel").json()["id"] == job["id"]
    gate.set()
    done = wait(client, job)
    assert done["status"] == "cancelled" and not done["cancellable"]
    assert client.get("/api/jobs").json() == []
    assert client.post("/api/jobs/nope/cancel").status_code == 404


def test_cancelled_write_keeps_records_consistent(client, guesser, batch, fake):
    calls = {"n": 0}
    real = guesser.immich.set_date

    def counting(*args, **kwargs):
        calls["n"] += 1
        real(*args, **kwargs)
        if calls["n"] == 1:  # the user clicks "Cancel" while the first date is being written
            for j in client.app.state.jobs.active():
                client.app.state.jobs.cancel(j.id)

    guesser.immich.set_date = counting
    job = client.post("/api/known-date", json={"asset_ids": batch["ids"], "date": "1986", "album_id": "scans"}).json()
    assert wait(client, job)["status"] == "cancelled"
    # Exactly the one photo that was written is recorded, tagged and in the history.
    assets = {a["id"]: a for a in client.get("/api/albums/scans/assets").json()["assets"]}
    dated = [aid for aid in batch["ids"] if assets[aid]["dated"]]
    assert len(dated) == 1 and fake.assets[dated[0]].tags == {"Metadatenmaster/Date/Manual"}
    assert client.get("/api/history").json()[0]["count"] == 1


def test_undo_cannot_be_cancelled(client, batch):
    wait(client, client.post("/api/known-date", json={"asset_ids": batch["ids"][:1], "date": "1986"}).json())
    job = client.post(f"/api/history/{client.get('/api/history').json()[0]['id']}/undo").json()
    assert not job["cancellable"]
    assert client.post(f"/api/jobs/{job['id']}/cancel").status_code == 400


def test_clock_candidates_and_camera_offsets_via_web(client, fake):
    client.post("/api/tracks", params={"file_name": "hike.gpx"}, content=HIKE)
    cam = fake.add_photo("IMG_0001.CR2", "2024-07-01T09:50:00")
    fake.add_photo("PXL_1.jpg", "2024-07-01T10:20:00", tz="Europe/Berlin", make="Google", model="Pixel 8",
                   lat=47.0, lon=11.0)
    r = client.get("/api/geo/clock-candidates", params={"asset_id": cam, "camera_tz": "Europe/Berlin"}).json()
    assert r["others"][0]["delta_s"] == 1800
    assert client.get("/api/geo/clock-candidates", params={"asset_id": cam, "camera_tz": "Mars"}).status_code == 400
    job = wait(client, client.post("/api/geo/find", json={
        "camera_tz": "Europe/Berlin", "use_nearby": False, "camera_offsets": {"Canon EOS 80D": 1800}}).json())
    assert job["status"] == "done"
    s = client.get("/api/geo/suggestions").json()["items"][0]
    assert s["id"] == cam and s["offset_s"] == 1800 and s["lat"] == pytest.approx(47.04)


def test_place_names(guesser):
    import httpx
    calls = []

    def photon(request):
        calls.append(request)
        return httpx.Response(200, json={"features": [{"properties": {"name": "Ort", "type": "city"}}]})

    keys = [f"47.{i:03d},11.000" for i in range(20)]
    with TestClient(create_app(guesser, geocode_transport=httpx.MockTransport(photon)),
                    headers={"X-Metadatenmaster": "1"}) as c:
        r = c.post("/api/place-names", json={"keys": keys + ["nonsense"], "lang": "de-DE"}).json()
        assert len(r["names"]) == 15 and r["pending"] == 5 and set(r["names"].values()) == {"Ort"}
        r = c.post("/api/place-names", json={"keys": keys, "lang": "de-DE"}).json()
        assert len(r["names"]) == 20 and r["pending"] == 0
        assert len(calls) == 20  # cached ones are not asked again
        guesser.settings = guesser.settings.with_values({"place_names": False})
    with TestClient(create_app(guesser), headers={"X-Metadatenmaster": "1"}) as c:
        assert c.post("/api/place-names", json={"keys": keys}).json()["enabled"] is False
