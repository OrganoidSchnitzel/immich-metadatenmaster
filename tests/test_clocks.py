from datetime import datetime, timedelta, timezone

import pytest

from immich_metadatenmaster.geotag import GeoOptions

UTC = timezone.utc
MOMENTS = ["10:00:00", "11:30:00", "13:05:00", "15:10:00", "16:40:00"]  # real Berlin times


def shifted(clock: str, delta: timedelta) -> str:
    return (datetime.fromisoformat(f"2024-07-01T{clock}") + delta).isoformat()


def day(**kw) -> GeoOptions:
    return GeoOptions(camera_tz="Europe/Berlin", use_tracks=False, start=datetime(2024, 7, 1, 6, tzinfo=UTC),
                      end=datetime(2024, 7, 1, 18, tzinfo=UTC), **kw)


@pytest.fixture
def trip(fake):
    """A Canon whose clock is 2 h 0 min 12 s behind, and a phone that took the same moments (and more)."""
    behind = timedelta(hours=-2, seconds=-12)
    ids = {"canon": [], "phone": []}
    for i, moment in enumerate(MOMENTS):
        for k in range(2):  # a burst of two shots
            c = fake.add_photo(f"IMG_{i}{k}.CR2", shifted(moment, behind + timedelta(seconds=20 * k)),
                               model="Canon EOS 77D")
            fake.scenes[c] = f"scene {i}"
            ids["canon"].append(c)
        p = fake.add_photo(f"PXL_{i}.jpg", shifted(moment, timedelta(seconds=40)), tz="Europe/Berlin",
                           make="Google", model="Pixel 8")
        fake.scenes[p] = f"scene {i}"
        ids["phone"].append(p)
    for minute in range(0, 600, 47):  # other phone photos through the day
        fake.add_photo(f"PXL_x{minute}.jpg", shifted("08:00:00", timedelta(minutes=minute)), tz="Europe/Berlin",
                       make="Google", model="Pixel 8")
    return ids


def test_finds_the_offset_from_shared_moments_and_similar_photos(geo, trip):
    result = geo.check_clocks(day())
    [canon] = [c for c in result["cameras"] if c["camera"] == "Canon EOS 77D"]
    assert result["smart_search"] and result["references"] > 5
    assert canon["confidence"] == "high" and abs(canon["offset_s"] - (2 * 3600 + 12 + 40)) <= 30
    assert canon["moments"] == 5 and canon["pairs_agreeing"] >= 3 and canon["current_s"] == 0
    example = canon["examples"][0]
    assert example["photo"]["file_name"].startswith("IMG_") and example["reference"]["file_name"].startswith("PXL_")


def test_times_alone_without_smart_search(fake, geo, trip):
    fake.smart_search = False
    result = geo.check_clocks(day())
    canon = result["cameras"][0]
    assert not result["smart_search"] and "not enabled" in result["smart_search_error"]
    assert canon["confidence"] == "medium" and abs(canon["offset_s"] - 7252) <= 30 and canon["pairs_checked"] == 10


def test_similar_photos_decide_when_the_phone_shot_all_day(fake, geo):
    # A photo every 4 minutes: shifted by almost any offset, the camera's photos meet a phone photo.
    for minute in range(0, 600, 4):
        fake.add_photo(f"PXL_{minute:03d}.jpg", shifted("08:00:00", timedelta(minutes=minute)),
                       tz="Europe/Berlin", make="Google", model="Pixel 8")
    phone = {a.file_name: a.id for a in fake.assets.values()}
    for i, minute in enumerate([60, 180, 300, 420]):
        c = fake.add_photo(f"IMG_{i}.CR2", shifted("08:00:00", timedelta(minutes=minute - 95)), model="Canon EOS 77D")
        fake.scenes[c] = fake.scenes[phone[f"PXL_{minute:03d}.jpg"]] = f"scene {i}"
    canon = geo.check_clocks(day())["cameras"][0]
    assert canon["offset_s"] == 95 * 60 and canon["confidence"] in {"medium", "high"}


def test_no_reference_photos(fake, geo):
    fake.add_photo("IMG_1.CR2", "2024-07-01T10:00:00", model="Canon EOS 77D")
    canon = geo.check_clocks(day())["cameras"][0]
    assert canon["offset_s"] is None and canon["confidence"] == "none"


def test_clock_check_via_web(fake, guesser, trip):
    from fastapi.testclient import TestClient

    from immich_metadatenmaster.web.app import create_app
    from test_web import wait
    with TestClient(create_app(guesser), headers={"X-Metadatenmaster": "1"}) as client:
        job = client.post("/api/geo/clock-check", json={"use_tracks": False, "start": "2024-07-01T08:00",
                                                        "end": "2024-07-01T20:00", "camera_tz": "Europe/Berlin"}).json()
        assert job["label"] == "Checking camera clocks"
        result = wait(client, job)["result"]
    assert result["cameras"][0]["camera"] == "Canon EOS 77D" and result["cameras"][0]["confidence"] == "high"


def test_cameras_that_store_a_time_zone_are_checked_too(fake, geo):
    # Newer DSLRs store the zone set in their menu, but their clock can still be off: 1 h behind here.
    for i, moment in enumerate(MOMENTS[:4]):
        c = fake.add_photo(f"IMG_{i}.CR3", shifted(moment, timedelta(hours=-1)), tz="Europe/Berlin",
                           model="Canon EOS R6")
        p = fake.add_photo(f"IMG2024070{i}.jpg", shifted(moment, timedelta(seconds=30)), tz="Europe/Berlin",
                           make="realme", model="GT 2")
        fake.scenes[c] = fake.scenes[p] = f"scene {i}"
    result = geo.check_clocks(day())
    assert [(d["camera"], d["phone"], d["photos"]) for d in result["devices"]] == \
        [("Canon EOS R6", False, 4), ("realme GT 2", True, 4)]
    [canon] = result["cameras"]
    assert canon["camera"] == "Canon EOS R6" and abs(canon["offset_s"] - 3630) <= 30


def test_is_phone():
    from immich_metadatenmaster.clocks import is_phone
    assert is_phone("realme", "GT 2") and is_phone("Apple", "iPhone 15") and is_phone("Sony", "XQ-DQ54")
    assert not is_phone("Canon", "Canon EOS 77D") and not is_phone("SONY", "ILCE-7M4") and not is_phone(None, None)


def test_the_search_finds_the_clock_by_itself(fake, geo, trip):
    """No offset entered: the Canon's 2 h are found and used, its photos get the phone's positions."""
    for p in trip["phone"]:
        fake.assets[p].latitude, fake.assets[p].longitude = 47.5, 11.1
    run = geo.find(day())
    canon = next(c for c in run["cameras"] if c["camera"] == "Canon EOS 77D")
    assert canon["clock"] == "detected" and canon["check"]["confidence"] == "high"
    assert abs(canon["offset_s"] - 7252) <= 30
    s = geo.suggestions()
    assert all(s[c]["status"] == "ok" and s[c]["source"] == "nearby" and s[c]["lat"] == pytest.approx(47.5)
               for c in trip["canon"])

    # An offset entered for the camera wins.
    run = geo.find(day(camera_offsets={"Canon EOS 77D": 60}))
    canon = next(c for c in run["cameras"] if c["camera"] == "Canon EOS 77D")
    assert (canon["clock"], canon["offset_s"]) == ("set", 60)
    geo.find(day())

    # Writing corrects the Canon's times (asked for per camera), in the local time of the place.
    first = trip["canon"][0]
    geo.apply(trip["canon"], fix_cameras=["Canon EOS 77D"])
    assert fake.assets[first].exif_date.startswith("2024-07-01T10:00:") and fake.assets[first].time_zone == \
        "Europe/Berlin"
    assert [b["source"] for b in geo.store.change_batches()][:2] == ["clock", "nearby"]


def test_no_clock_check_without_phone_photos(fake, geo):
    fake.add_photo("IMG_1.CR2", "2024-07-01T10:00:00", model="Canon EOS 77D")
    run = geo.find(day())
    canon = run["cameras"][0]
    assert canon["clock"] == "unknown" and canon["offset_s"] == 0 and canon["check"]["confidence"] == "none"
    assert next(c for c in geo.find(day(auto_clocks=False))["cameras"])["clock"] == "not checked"
