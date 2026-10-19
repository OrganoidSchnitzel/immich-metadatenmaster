from datetime import date, datetime, timezone

import pytest

from immich_metadatenmaster.geotag import GeoOptions, photo_instant, sibling_key

from conftest import gpx

UTC = timezone.utc
# A hike on 1 July 2024, 08:00-10:00 UTC (10:00-12:00 in Berlin), heading north one point every 10 min.
HIKE = [(47.0 + i * 0.01, 11.0, f"2024-07-01T{8 + i // 6:02d}:{i % 6 * 10:02d}:00Z") for i in range(13)]


@pytest.fixture
def tour(geo):
    return geo.add_track("hike.gpx", gpx(HIKE, name="Hike"))


def berlin(**kw) -> GeoOptions:
    return GeoOptions(camera_tz="Europe/Berlin", **kw)


def test_sibling_key():
    assert sibling_key("IMG_1234.CR2") == sibling_key("IMG_1234-HDR.dng") == sibling_key("img_1234_edited.jpg")
    assert sibling_key("IMG_1234.CR2") != sibling_key("IMG_1235.CR2")


def test_photo_instant_uses_camera_zone_unless_the_photo_has_one(fake, guesser):
    from zoneinfo import ZoneInfo
    naive = guesser.immich.asset(fake.add_photo("a.cr2", "2024-07-01T10:30:00"))
    aware = guesser.immich.asset(fake.add_photo("b.jpg", "2024-07-01T10:30:00", tz="Europe/London"))
    berlin_tz = ZoneInfo("Europe/Berlin")
    assert photo_instant(naive, berlin_tz, True) == datetime(2024, 7, 1, 8, 30, tzinfo=UTC)
    assert photo_instant(aware, berlin_tz, True) == datetime(2024, 7, 1, 9, 30, tzinfo=UTC)
    assert photo_instant(aware, berlin_tz, False) == datetime(2024, 7, 1, 8, 30, tzinfo=UTC)


def test_track_summary(geo, tour):
    assert tour["name"] == "Hike" and tour["points"] == 13 and tour["has_times"] and not tour["needs_times"]
    assert tour["start"] == "2024-07-01T08:00:00+00:00" and tour["end"] == "2024-07-01T10:00:00+00:00"
    assert tour["distance_km"] == pytest.approx(13.3, abs=0.1)
    assert [t["id"] for t in geo.tracks()] == [tour["id"]]


def test_suggestions_from_track_sibling_and_nothing_written(fake, geo, tour):
    a = fake.add_photo("IMG_0001.CR2", "2024-07-01T10:35:00")
    b = fake.add_photo("IMG_0002.CR2", "2024-07-01T11:00:00")
    raw = fake.add_photo("IMG_0003.CR2", "2024-07-01T11:10:00", lat=46.5, lon=10.5)
    hdr = fake.add_photo("IMG_0003-HDR.dng", "2024-07-01T11:10:00")
    later = fake.add_photo("IMG_0004.CR2", "2024-07-01T15:00:00")
    fake.add_photo("phone.jpg", "2024-07-01T10:36:00", tz="Europe/Berlin", lat=48.0, lon=12.0)

    run = geo.find(berlin())
    s = geo.suggestions()
    assert run["candidates"] == 3 and run["suggested"] == 3 and run["sources"] == {"gpx": 2, "sibling": 1}
    assert s[a]["source"] == "gpx" and s[a]["lat"] == pytest.approx(47.035) and s[a]["detail"] == "Hike"
    assert s[b]["lat"] == pytest.approx(47.06) and s[b]["confidence"] == "high"
    assert s[hdr]["source"] == "sibling" and (s[hdr]["lat"], s[hdr]["lon"]) == (46.5, 10.5)
    assert raw not in s and later not in s  # has a location / outside the track's time span
    assert fake.location_updates == [] and fake.date_updates == []


def test_nearby_photos_in_a_time_range(fake, geo):
    cam = fake.add_photo("IMG_0010.CR2", "2024-07-01T15:00:00")
    lonely = fake.add_photo("IMG_0011.CR2", "2024-07-01T18:00:00")
    fake.add_photo("phone1.jpg", "2024-07-01T14:56:00", tz="Europe/Berlin", lat=48.0, lon=12.0)
    fake.add_photo("phone2.jpg", "2024-07-01T15:04:00", tz="Europe/Berlin", lat=48.2, lon=12.0)
    geo.find(berlin(use_tracks=False, start=datetime(2024, 7, 1, tzinfo=UTC), end=datetime(2024, 7, 2, tzinfo=UTC)))
    s = geo.suggestions()
    assert s[cam]["source"] == "nearby" and s[cam]["lat"] == pytest.approx(48.1) and s[cam]["time_diff_s"] == 240
    assert s[lonely]["status"] == "no_match"


def test_clock_offset_and_time_fix(fake, geo, tour):
    # The camera ran 30 minutes slow: its 10:05 is really 10:35.
    a = fake.add_photo("IMG_0001.CR2", "2024-07-01T10:05:00")
    geo.find(berlin(offset_s=1800))
    s = geo.suggestions()[a]
    assert s["lat"] == pytest.approx(47.035) and s["corrected"] == "2024-07-01T10:35:00.000+02:00"

    result = geo.apply([a], fix_time=True)
    assert result == {"written": 1, "skipped": 0, "times_fixed": 1}
    assert (fake.assets[a].latitude, fake.assets[a].longitude) == (pytest.approx(47.035), 11.0)
    assert fake.assets[a].exif_date == "2024-07-01T10:35:00.000+02:00"
    assert fake.assets[a].tags == {"Metadatenmaster/Location/GPX"}
    assert geo.suggestions() == {}

    batches = geo.store.change_batches()
    assert [(b["kind"], b["source"]) for b in batches] == [("date", "clock"), ("location", "gpx")]


def test_time_zone_hint(fake, geo, tour):
    for i, local in enumerate(["10:02", "10:30", "11:55"]):
        fake.add_photo(f"IMG_{i}.CR2", f"2024-07-01T{local}:00")
    run = geo.find(GeoOptions(camera_tz="UTC"))  # wrong: the camera showed Berlin time
    assert run["hint"] == {"hours": -2, "matches": 3, "current": 1}
    assert geo.find(berlin())["hint"] is None


def test_route_without_times_needs_times(geo, fake):
    plan = geo.add_track("plan.gpx", gpx([(47.0, 11.0, None), (47.1, 11.0, None)], name="Plan"))
    assert plan["needs_times"]
    with pytest.raises(ValueError, match="Select at least one"):
        geo.find(berlin())
    geo.set_track_times(plan["id"], datetime(2024, 7, 1, 8, tzinfo=UTC), datetime(2024, 7, 1, 10, tzinfo=UTC))
    a = fake.add_photo("IMG_1.CR2", "2024-07-01T11:00:00")
    geo.find(berlin())
    s = geo.suggestions()[a]
    assert s["lat"] == pytest.approx(47.05) and s["confidence"] == "low"  # only an assumed pace


def test_place_on_map_and_discard(fake, geo, tour):
    a = fake.add_photo("IMG_1.CR2", "2024-07-01T10:30:00")
    b = fake.add_photo("IMG_2.CR2", "2024-07-01T10:40:00")
    geo.find(berlin())
    assert geo.place([a], 45.5, 9.25) == 1
    geo.discard([b])
    s = geo.suggestions()
    assert set(s) == {a} and (s[a]["source"], s[a]["lat"]) == ("map", 45.5)
    geo.apply([a, b])
    assert fake.assets[a].tags == {"Metadatenmaster/Location/Map"} and fake.assets[b].latitude is None


def test_album_filter(fake, geo, tour):
    inside = fake.add_photo("IMG_1.CR2", "2024-07-01T10:30:00", album="trip")
    fake.add_photo("IMG_2.CR2", "2024-07-01T10:40:00")
    geo.find(berlin(album_id="trip"))
    assert set(geo.suggestions()) == {inside}


def test_undo_of_locations_is_not_possible_but_dates_are(fake, geo, guesser, tour):
    a = fake.add_photo("IMG_1.CR2", "2024-07-01T10:30:00")
    geo.find(berlin())
    geo.apply([a])
    loc = guesser.store.change_batch(guesser.store.change_batches()[0]["id"])
    assert not geo.history.undoable(loc)

    from immich_metadatenmaster.dates import parse_date_spec
    guesser.apply_known_date([a], parse_date_spec("1999"))
    batch = guesser.store.change_batch(guesser.store.change_batches()[0]["id"])
    assert batch["kind"] == "date" and geo.history.undoable(batch)
    assert fake.assets[a].exif_date.startswith("1999")
    assert geo.history.undo(batch["id"]) == {"restored": 1, "skipped": 0}
    assert datetime.fromisoformat(fake.assets[a].exif_date).astimezone(UTC) == datetime(2024, 7, 1, 10, 30, tzinfo=UTC)
    assert "Metadatenmaster/Date/Manual" not in fake.assets[a].tags
    assert guesser.store.dated([a]) == {}
    with pytest.raises(ValueError, match="already undone"):
        geo.history.undo(batch["id"])


def test_known_date_records_history(fake, guesser):
    aid = fake.add_asset("scan.jpg", date(1987, 1, 1), [])
    from immich_metadatenmaster.dates import parse_date_spec
    guesser.apply_known_date([aid], parse_date_spec("06.1987"))
    batch = guesser.store.change_batch(guesser.store.change_batches()[0]["id"])
    assert batch["label"] == "Known date 1987-06" and batch["tag"] == "Metadatenmaster/Date/Manual"
    change = batch["changes"][0]
    assert change["file_name"] == "scan.jpg" and change["new"]["date"].startswith("1987-06-15T12:00")
    assert change["old"]["taken_at"] == "2026-09-01T10:00:00+00:00"
