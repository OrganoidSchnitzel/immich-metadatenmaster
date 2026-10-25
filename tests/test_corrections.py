"""Check & fix: wrong camera clocks, locations that do not fit, home time instead of local time."""

from datetime import datetime, timedelta, timezone

import pytest

from immich_metadatenmaster.corrections import CheckOptions, Corrections

from conftest import gpx

UTC = timezone.utc
# A hike on 1 July 2024, 08:00-10:00 UTC (10:00-12:00 in Berlin), heading north 0.01° every 10 min.
HIKE = [(47.0 + i * 0.01, 11.0, f"2024-07-01T{8 + i // 6:02d}:{i % 6 * 10:02d}:00Z") for i in range(13)]
# Real Berlin times of moments photographed with the camera and the phone.
MOMENTS = ["10:05", "10:35", "11:05", "11:35"]


def on_hike(berlin: str) -> float:
    """The hike's latitude at a Berlin time."""
    h, m = map(int, berlin.split(":"))
    return 47.0 + ((h - 10) * 60 + m) / 10 * 0.01


@pytest.fixture
def checker(guesser, geo):
    return Corrections(guesser.settings, guesser.immich, guesser.store, geo)


@pytest.fixture
def hike(fake, geo):
    """The Canon's clock is 30 min behind. Two of its photos got a location with the wrong time, two have none."""
    geo.add_track("hike.gpx", gpx(HIKE, name="Hike"))
    canon: list[str] = []
    for i, moment in enumerate(MOMENTS):
        real = datetime.fromisoformat(f"2024-07-01T{moment}:00")
        shown = (real - timedelta(minutes=30)).isoformat()
        wrong = on_hike((real - timedelta(minutes=30)).strftime("%H:%M")) if i < 2 else None
        c = fake.add_photo(f"IMG_{i}.CR2", shown, model="Canon EOS 77D", lat=wrong, lon=11.0 if wrong else None)
        p = fake.add_photo(f"PXL_{i}.jpg", real.isoformat(), tz="Europe/Berlin", make="Google", model="Pixel 8",
                           lat=on_hike(moment), lon=11.0)
        fake.scenes[c] = fake.scenes[p] = f"scene {i}"
        canon.append(c)
    return canon


def berlin(**kw) -> CheckOptions:
    return CheckOptions(camera_tz="Europe/Berlin", **kw)


def test_a_wrong_camera_clock_and_its_consequences(fake, checker, hike, guesser):
    result = checker.scan(berlin())
    [trip] = result["trips"]
    assert trip["label"] == "Hike"
    assert next(c for c in trip["cameras"] if c["camera"] == "Canon EOS 77D")["confidence"] == "high"
    kinds = {f["kind"]: f for f in result["findings"]}
    assert set(kinds) == {"clock", "move_track", "add_track"}  # the phone is right, and left alone

    clock = kinds["clock"]
    assert clock["title"] == "Canon EOS 77D: clock 30 min behind" and clock["count"] == 4
    assert clock["confidence"] == "high" and clock["examples"]
    move = kinds["move_track"]
    assert move["count"] == 2 and "median 3.3 km away" in move["text"]
    assert {i["asset_id"] for i in move["items"]} == set(hike[:2])
    assert kinds["add_track"]["count"] == 2

    # Fixing the clock: the real time, in the local time of the place.
    assert checker.apply(clock["id"], hike)["written"] == 4
    assert fake.assets[hike[0]].exif_date == "2024-07-01T10:05:00.000+02:00"
    assert fake.assets[hike[0]].time_zone == "Europe/Vienna"  # the hike is in Tyrol
    # Moving the locations onto the track.
    assert checker.apply(move["id"], hike[:2])["written"] == 2
    assert fake.assets[hike[0]].latitude == pytest.approx(on_hike("10:05"))
    assert "Metadatenmaster/Location/GPX" in fake.assets[hike[0]].tags
    batches = guesser.store.change_batches()
    assert [(b["kind"], b["source"], b["label"]) for b in batches[:2]] == [
        ("location", "gpx", "Location: GPX track (moved by Check & fix)"),
        ("date", "clock", "Clock of Canon EOS 77D corrected by +0:30:00")]

    # Fixed: only the photos without a location are left, and they stay found after another check.
    left = checker.results()["findings"]
    assert [f["kind"] for f in left] == ["add_track"]
    again = checker.scan(berlin())
    assert [f["kind"] for f in again["findings"]] == ["add_track"]

    # Undo puts the times back.
    guesser.history.undo(batches[1]["id"])
    assert fake.assets[hike[0]].local_datetime.startswith("2024-07-01T09:35")


def test_dismiss(checker, hike):
    result = checker.scan(berlin())
    move = next(f for f in result["findings"] if f["kind"] == "move_track")
    assert checker.dismiss(move["id"], [hike[0]]) == 1
    assert next(f for f in checker.results()["findings"] if f["kind"] == "move_track")["count"] == 1
    checker.dismiss(move["id"])
    assert "move_track" not in {f["kind"] for f in checker.results()["findings"]}
    with pytest.raises(ValueError):
        checker.apply("nope", hike)


def test_hand_placed_far_from_the_phone(fake, checker):
    """A day in Mallorca: the camera photos were all put on the middle of the island by hand."""
    for i, clock in enumerate(["10:00", "14:00", "18:00"]):  # the phone's GPS: never exactly the same point
        fake.add_photo(f"PXL_{clock}.jpg", f"2024-05-02T{clock}:00", tz="Europe/Madrid", make="Google",
                       model="Pixel 8", lat=39.57 + i * 0.0001, lon=2.65)
    placed = [fake.add_photo(f"DSC_{clock}.JPG", f"2024-05-02T{clock}:00", make="Sony", model="ILCE-7M3",
                             lat=39.6, lon=3.0) for clock in ["10:02", "14:03", "18:01"]]
    result = checker.scan(berlin(use_tracks=False, start=datetime(2024, 5, 2, tzinfo=UTC),
                                 end=datetime(2024, 5, 3, tzinfo=UTC)))
    [finding] = result["findings"]
    assert finding["kind"] == "move_nearby" and finding["count"] == 3 and "30." in finding["text"]
    assert result["trips"][0]["label"] == "Time range"
    checker.apply(finding["id"], placed)
    assert (fake.assets[placed[0]].latitude, fake.assets[placed[0]].longitude) == (pytest.approx(39.57), 2.65)


def test_home_time_instead_of_local_time(fake, checker, guesser):
    """A camera still on German time in Colorado, its clock otherwise right: its 21:45 was 13:45 there."""
    cams = []
    for i, denver in enumerate(["13:45", "14:10", "14:30", "15:05"]):
        h, m = map(int, denver.split(":"))
        home = f"2022-08-25T{h + 8:02d}:{m:02d}:00"
        c = fake.add_photo(f"IMG_{i}.JPG", home, model="Canon EOS 77D", lat=40.3 + i * 0.01, lon=-105.69)
        p = fake.add_photo(f"PXL_{i}.jpg", f"2022-08-25T{denver}:20", tz="America/Denver", make="Google",
                           model="Pixel 6", lat=40.3 + i * 0.01, lon=-105.69)
        fake.scenes[c] = fake.scenes[p] = f"scene {i}"
        cams.append(c)
    result = checker.scan(berlin(use_tracks=False, start=datetime(2022, 8, 25, 12, tzinfo=UTC),
                                 end=datetime(2022, 8, 26, 6, tzinfo=UTC)))
    [finding] = result["findings"]
    assert finding["kind"] == "local_time" and finding["count"] == 4
    assert finding["title"] == "Canon EOS 77D: 4 photos shown in Europe/Berlin time"
    assert finding["items"][0]["after"]["local"] == "2022-08-25T13:45:00"
    checker.apply(finding["id"], cams)
    assert (fake.assets[cams[0]].exif_date, fake.assets[cams[0]].time_zone) == ("2022-08-25T13:45:00.000-06:00",
                                                                                "America/Denver")
    assert guesser.store.change_batches()[0]["source"] == "localtime"


def test_nothing_to_check(checker):
    with pytest.raises(ValueError):
        checker.scan(berlin())
    assert checker.results() is None


def test_check_via_web(fake, guesser, hike):
    from fastapi.testclient import TestClient

    from immich_metadatenmaster.web.app import create_app
    from test_web import wait

    with TestClient(create_app(guesser), headers={"X-Metadatenmaster": "1"}) as c:
        assert "Check &amp; fix" in c.get("/check").text
        assert c.get("/api/check").json() == {"created_at": None}
        job = c.post("/api/check", json={}).json()
        assert job["meta"] == {"page": "check"} and wait(c, job)["status"] == "done"
        data = c.get("/api/check").json()
        clock = next(f for f in data["findings"] if f["kind"] == "clock")
        job = c.post("/api/check/fix", json={"finding_id": clock["id"], "asset_ids": hike}).json()
        assert wait(c, job)["result"] == {"written": 4, "skipped": 0}
        assert c.post("/api/check/dismiss", json={"finding_id": "0:add_track:Canon EOS 77D"}).json() == {"dismissed": 2}
        assert [f["kind"] for f in c.get("/api/check").json()["findings"]] == ["move_track"]
        assert c.post("/api/check", json={"start": "2024-07-01T10:00"}).status_code == 400
