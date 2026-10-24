from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from immich_metadatenmaster.dates import parse_date_spec
from immich_metadatenmaster.geotag import GeoOptions
from immich_metadatenmaster.jobs import JobCancelled
from immich_metadatenmaster.overview import LibraryScan, list_title


@pytest.fixture
def lib(guesser):
    return LibraryScan(guesser.immich, guesser.store)


@pytest.fixture
def library(fake):
    """A small library, by role."""
    ids = {
        "located": fake.add_photo("IMG_0001.CR2", "2019-07-10T10:00:00", lat=47.4, lon=10.9),
        "unlocated": fake.add_photo("IMG_0002.CR2", "2019-07-11T10:00:00"),
        # No capture date: Immich files these under the day they were scanned or saved.
        "scan": fake.add_photo("Scan 001.jpg", "2024-03-05T10:00:00", exif_date=False, make=None, model=None),
        "whatsapp": fake.add_photo("IMG-20190713-WA0001.jpg", "2024-03-05T11:00:00", exif_date=False),
        # The name confirms the date Immich shows; a screenshot needs no location.
        "whatsapp_ok": fake.add_photo("IMG-20190714-WA0002.jpg", "2019-07-14T09:00:00", exif_date=False),
        "screenshot": fake.add_photo("Screenshot_20190715-120000.png", "2019-07-15T12:00:00", make=None, model=None),
        "live_video": fake.add_photo("IMG_0003.MOV", "2019-07-12T10:00:00"),
    }
    fake.assets[ids["live_video"]].type = "VIDEO"
    fake.assets[ids["live_video"]].visibility = "hidden"  # the video half of a Live Photo
    return ids


def numbers(row):
    return row["total"], row["no_location"], row["no_date"], row["name_date"]


def test_scan_counts_what_is_missing_per_month(fake, lib, library):
    assert lib.summary() is None
    progress = []
    s = lib.scan(progress=lambda done, total: progress.append((done, total)))
    assert numbers(s) == (6, 4, 1, 1)
    assert [y["year"] for y in s["years"]] == ["2024", "2019"]
    assert s["years"][0]["months"] == [{"month": "2024-03", "label": "March 2024", "total": 2, "no_location": 2,
                                        "no_date": 1, "name_date": 1}]
    assert numbers(s["years"][1]) == (4, 2, 0, 0)
    assert progress == [(7, 7)]  # every asset read (the hidden one too), out of Immich's count

    fake.denied_paths.add("/search/statistics")  # an API key without asset.statistics
    progress.clear()
    assert lib.scan(progress=lambda done, total: progress.append((done, total)))["total"] == 6
    assert progress == [(7, 7)]


def test_a_cancelled_scan_keeps_the_previous_one(fake, lib, library):
    first = lib.scan()
    fake.add_photo("IMG_9999.CR2", "2020-01-01T10:00:00")

    def cancel(done, total):
        raise JobCancelled()

    with pytest.raises(JobCancelled):
        lib.scan(progress=cancel)
    assert lib.summary() == first


def test_writes_keep_the_overview_up_to_date(fake, guesser, geo, lib, library):
    lib.scan()
    guesser.apply_known_date([library["scan"]], parse_date_spec("07.1987"))
    berlin = ZoneInfo("Europe/Berlin")
    geo.find(GeoOptions(use_tracks=False, start=datetime(2019, 7, 11, tzinfo=berlin),
                        end=datetime(2019, 7, 12, tzinfo=berlin), camera_tz="Europe/Berlin"))
    geo.place([library["unlocated"]], 47.5, 11.0, "Garmisch")
    assert geo.apply([library["unlocated"]])["written"] == 1
    geo.mark_no_location([library["whatsapp"]])

    s = lib.summary()
    assert numbers(s) == (6, 2, 0, 1)
    # The scan was dated, so it moved to July 1987 (it still has no location).
    assert [y["year"] for y in s["years"]] == ["2024", "2019", "1987"]
    assert numbers(s["years"][2]) == (1, 1, 0, 0) and numbers(s["years"][0]) == (1, 0, 0, 1)
    # A new scan finds the same.
    rescan = lib.scan()
    assert {k: v for k, v in rescan.items() if k != "scanned_at"} == {k: v for k, v in s.items() if k != "scanned_at"}


def test_a_date_chosen_by_the_user_beats_the_file_name(fake, guesser, lib):
    aid = fake.add_photo("2019-07-01 Scan 001.jpg", "2024-03-05T10:00:00", exif_date=False, make=None, model=None)
    assert lib.scan()["name_date"] == 1  # the name holds the day it was scanned
    guesser.apply_known_date([aid], parse_date_spec("1987"))
    assert lib.summary()["name_date"] == 0
    assert lib.scan()["name_date"] == 0


def test_lists_open_like_albums_and_stay_current(fake, lib, library):
    lib.scan()
    assert list_title("scan:no-date:2024-03") == "No capture date · March 2024"
    assert [a.id for a in lib.list_assets("scan:no-date:2024-03")] == [library["scan"]]
    # Most of the month is needed: it is read in one search (a day of slack for time zones), with people.
    window = fake.search_calls[-1]
    assert (window["takenAfter"], window["takenBefore"], window["withPeople"]) == \
        ("2024-02-29T00:00:00.000Z", "2024-04-02T00:00:00.000Z", True)
    assert [a.id for a in lib.list_assets("scan:name-date:2024")] == [library["whatsapp"]]

    # Dated in Immich itself, and deleted: both leave their lists, and the numbers follow.
    scan = fake.assets[library["scan"]]
    scan.exif_date = scan.local_datetime = "1990-01-01T12:00:00.000Z"
    del fake.assets[library["whatsapp"]]
    assert lib.list_assets("scan:no-date:2024-03") == []
    assert lib.list_assets("scan:name-date:2024") == []
    s = lib.summary()
    assert numbers(s) == (5, 3, 0, 0) and [y["year"] for y in s["years"]] == ["2019", "1990"]

    with pytest.raises(ValueError):
        lib.list_assets("scan:no-date:2024-13")


def test_lists_read_only_what_they_need(fake, lib):
    for i in range(120):
        fake.add_photo(f"IMG_{i:04d}.JPG", "2019-07-10T10:00:00", lat=47.0, lon=11.0)
    scan = fake.add_photo("Scan A.jpg", "2019-07-20T10:00:00", exif_date=False, make=None, model=None)
    march = [fake.add_photo(f"Scan {n}.jpg", "2019-03-02T10:00:00", exif_date=False, make=None, model=None)
             for n in "BC"]
    lib.scan()
    fake.search_calls.clear()
    # One photo of a busy month: asked for by itself.
    assert [a.id for a in lib.list_assets("scan:no-date:2019-07")] == [scan]
    assert fake.search_calls == []
    # The whole year: only the month where most photos are needed is searched.
    assert {a.id for a in lib.list_assets("scan:no-date:2019")} == {scan, *march}
    assert [(c["takenAfter"], c["takenBefore"]) for c in fake.search_calls] == \
        [("2019-02-28T00:00:00.000Z", "2019-04-02T00:00:00.000Z")]


def test_overview_via_web(client, fake, library):
    from test_web import wait

    assert 'id="library"' in client.get("/").text
    assert client.get("/api/library").json() == {"scanned_at": None}
    job = client.post("/api/library/scan").json()
    assert job["label"] == "Scanning the library" and job["meta"] == {"href": "/#library"}
    assert wait(client, job)["result"]["total"] == 6
    assert client.get("/api/library").json()["no_date"] == 1

    page = client.get("/album/scan:no-date:2024-03").text
    assert "No capture date · March 2024" in page and "Library overview" in page
    assert "remove-from-album" not in page and "/albums/scan:" not in page
    assert client.get("/album/scan:no-date:2024-13").status_code == 404
    assert client.get("/api/albums/scan:nothing:2024/assets").status_code == 404
    items = client.get("/api/albums/scan:no-date:2024-03/assets").json()["assets"]
    assert [i["id"] for i in items] == [library["scan"]]

    # Dating a photo of a list: there is no album to remove it from, and it leaves the list.
    job = client.post("/api/known-date", json={"asset_ids": [library["scan"]], "date": "1987",
                                               "album_id": "scan:no-date:2024-03", "remove_from_album": True}).json()
    done = wait(client, job)
    assert done["status"] == "done" and set(done["result"]) == {"written", "date", "label"}
    assert client.get("/api/albums/scan:no-date:2024-03/assets").json()["assets"] == []

    job = client.post("/api/name-dates", json={"album_id": "scan:name-date:2024-03",
                                               "asset_ids": [library["whatsapp"]]}).json()
    assert wait(client, job)["result"]["suggested"] == 1


def test_phone_names_in_utc_are_not_another_date(fake, lib):
    # Pixel phones name files by UTC time: taken at 10:20:05 in Berlin.
    fake.add_photo("PXL_20240701_082005123.jpg", "2024-07-01T10:20:05", tz="Europe/Berlin", lat=47.0, lon=11.0,
                   make="Google", model="Pixel 8")
    assert lib.scan()["name_date"] == 0


@pytest.fixture
def client(guesser):
    from fastapi.testclient import TestClient

    from immich_metadatenmaster.web.app import create_app
    with TestClient(create_app(guesser), headers={"X-Metadatenmaster": "1"}) as c:
        yield c
