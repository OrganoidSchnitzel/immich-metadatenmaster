import pytest

from immich_metadatenmaster.versions import VersionDates, is_version


@pytest.fixture
def versions(guesser):
    v = VersionDates(guesser.settings, guesser.immich, guesser.store)
    v.history.clear_poll_s = v.history.clear_timeout_s = 0
    return v


def test_is_version():
    assert is_version("IMG_1234-HDR.dng") and is_version("IMG_1234_edited.jpg") and is_version("IMG_1234 (1).jpg")
    assert not is_version("IMG_1234.CR2") and not is_version("Urlaub 1.jpg") and not is_version("Credit card.jpg")


def test_hdr_gets_date_and_location_of_its_raw(fake, versions):
    raw = fake.add_photo("IMG_1234.CR2", "2024-07-01T10:31:00", lat=47.4, lon=10.9)
    fake.add_photo("IMG_1234.JPG", "2024-07-01T10:31:00")  # a second original of the same shot, no location
    # Merged a week later; the merge lost the capture date, so Immich shows the export date.
    hdr = fake.add_photo("IMG_1234-HDR.dng", "2024-07-10T18:22:00", exif_date=False)
    fine = fake.add_photo("IMG_2000_edited.jpg", "2024-07-01T11:00:00")
    fake.add_photo("IMG_2000.CR2", "2024-07-01T11:00:00")

    assert versions.find() == {"versions": 2, "suggested": 1, "ambiguous": 0, "without_original": 0}
    s = versions.suggestions()[hdr]
    assert s["original_name"] == "IMG_1234.CR2" and not s["has_date"]
    assert s["new"] == "2024-07-01T10:31:00" and s["stamp"] == "2024-07-01T10:31:00.000+02:00"
    # The location comes from whichever original of the shot has one.
    assert (s["latitude"], s["longitude"], s["location_from"]) == (47.4, 10.9, fake.assets[raw].file_name)
    assert fine not in versions.suggestions() and fake.date_updates == []

    assert versions.apply([hdr]) == {"written": 1, "locations": 1, "skipped": 0}
    a = fake.assets[hdr]
    assert a.exif_date == "2024-07-01T10:31:00.000+02:00" and (a.latitude, a.longitude) == (47.4, 10.9)
    assert a.tags == {"Metadatenmaster/Date/From original", "Metadatenmaster/Location/Same-name file"}
    assert versions.suggestions() == {}
    labels = [b["label"] for b in versions.store.change_batches()]
    assert labels == ["Location: Same-name file", "Dates copied from originals"]


def test_counter_wrap_uses_the_original_in_the_same_folder(fake, versions):
    fake.add_photo("IMG_1234.CR2", "2019-05-01T09:00:00", folder="2019")
    fake.add_photo("IMG_1234.CR2", "2024-07-01T10:31:00", folder="2024")
    hdr = fake.add_photo("IMG_1234-HDR.dng", "2024-07-10T18:22:00", exif_date=False, folder="2024")
    other = fake.add_photo("IMG_1234-pano.dng", "2024-07-10T18:30:00", exif_date=False, folder="elsewhere")
    result = versions.find()
    assert result["suggested"] == 1 and result["ambiguous"] == 1
    assert versions.suggestions()[hdr]["new"] == "2024-07-01T10:31:00" and other not in versions.suggestions()


def test_album_scope_and_missing_original(fake, versions):
    fake.add_photo("IMG_9999-HDR.dng", "2024-07-10T18:22:00", exif_date=False, album="trip")
    fake.add_photo("IMG_5555-HDR.dng", "2024-07-10T18:22:00", exif_date=False)  # not in the album
    assert versions.find("trip") == {"versions": 1, "suggested": 0, "ambiguous": 0, "without_original": 1}


def test_versions_via_web(client, fake):
    fake.add_photo("IMG_1234.CR2", "2024-07-01T10:31:00")
    hdr = fake.add_photo("IMG_1234-HDR.dng", "2024-07-10T18:22:00", exif_date=False)
    job = client.post("/api/versions/find", json={}).json()
    assert job["label"] == "Looking for versions"
    from test_web import wait
    assert wait(client, job)["result"]["suggested"] == 1
    items = client.get("/api/versions").json()
    assert [i["id"] for i in items] == [hdr] and items[0]["original_name"] == "IMG_1234.CR2"
    result = wait(client, client.post("/api/versions/apply", json={"asset_ids": [hdr]}).json())["result"]
    assert result == {"written": 1, "locations": 0, "skipped": 0}
    assert client.get("/api/versions").json() == []
    history = client.get("/api/history").json()
    assert history[0]["label"] == "Dates copied from originals" and history[0]["undoable"]


@pytest.fixture
def client(guesser):
    from fastapi.testclient import TestClient

    from immich_metadatenmaster.web.app import create_app
    with TestClient(create_app(guesser), headers={"X-Metadatenmaster": "1"}) as c:
        yield c
