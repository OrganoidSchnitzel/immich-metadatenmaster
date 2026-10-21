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
    with pytest.raises(ValueError, match="Select a GPX track"):
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


UNDO = {"restored": 1, "skipped": 0, "changed": 0, "failed": 0}


def last_batch(store):
    return store.change_batch(store.change_batches()[0]["id"])


def test_undo_location_clears_it_again(fake, geo, guesser, tour):
    a = fake.add_photo("IMG_1.CR2", "2024-07-01T10:30:00")
    geo.find(berlin())
    geo.apply([a])
    batch = last_batch(guesser.store)
    assert batch["kind"] == "location" and geo.history.undoable(batch)
    assert geo.history.undo(batch["id"]) == UNDO
    assert fake.assets[a].latitude is None and fake.location_updates[-1]["latitude"] == 0
    assert "Metadatenmaster/Location/GPX" not in fake.assets[a].tags


def test_undo_location_restores_the_old_one(fake, geo, guesser):
    a = fake.add_photo("IMG_1.CR2", "2024-07-01T10:30:00", lat=46.0, lon=9.0)
    geo.find(berlin(use_tracks=False, album_id=None, include_located=True,
                    start=datetime(2024, 7, 1, tzinfo=UTC), end=datetime(2024, 7, 2, tzinfo=UTC)))
    assert geo.suggestions()[a]["status"] == "located"
    geo.place([a], 47.5, 10.5, "Füssen")
    geo.apply([a])
    assert (fake.assets[a].latitude, fake.assets[a].longitude) == (47.5, 10.5)
    assert geo.history.undo(last_batch(guesser.store)["id"]) == UNDO
    assert (fake.assets[a].latitude, fake.assets[a].longitude) == (46.0, 9.0)


def test_undo_location_when_immich_keeps_null_island(fake, geo, guesser, tour):
    fake.keep_null_island = True
    a = fake.add_photo("IMG_1.CR2", "2024-07-01T10:30:00")
    geo.find(berlin())
    geo.apply([a])
    result = geo.history.undo(last_batch(guesser.store)["id"])
    assert result == {**UNDO, "restored": 0, "failed": 1}
    assert fake.assets[a].latitude == pytest.approx(47.03)  # never left at (0, 0)


def test_undo_leaves_photos_changed_since_alone(fake, geo, guesser, tour):
    a = fake.add_photo("IMG_1.CR2", "2024-07-01T10:30:00")
    geo.find(berlin())
    geo.apply([a])
    fake.assets[a].latitude, fake.assets[a].longitude = 40.0, 5.0  # someone changed it in Immich
    assert geo.history.undo(last_batch(guesser.store)["id"]) == {**UNDO, "restored": 0, "changed": 1}
    assert fake.assets[a].latitude == 40.0


def test_undo_skips_deleted_photos(fake, geo, guesser, tour):
    a = fake.add_photo("IMG_1.CR2", "2024-07-01T10:30:00")
    geo.find(berlin())
    geo.apply([a])
    del fake.assets[a]
    fake.denied_paths.add(f"/assets/{a}")  # Immich answers with an error for a deleted photo
    assert geo.history.undo(last_batch(guesser.store)["id"]) == {**UNDO, "restored": 0, "changed": 1}


def test_undo_dates(fake, geo, guesser):
    a = fake.add_photo("IMG_1.CR2", "2024-07-01T10:30:00")
    from immich_metadatenmaster.dates import parse_date_spec
    guesser.apply_known_date([a], parse_date_spec("1999"))
    batch = last_batch(guesser.store)
    assert batch["kind"] == "date" and geo.history.undoable(batch)
    assert fake.assets[a].exif_date.startswith("1999")
    assert geo.history.undo(batch["id"]) == UNDO
    assert datetime.fromisoformat(fake.assets[a].exif_date).astimezone(UTC) == datetime(2024, 7, 1, 10, 30, tzinfo=UTC)
    assert "Metadatenmaster/Date/Manual" not in fake.assets[a].tags
    assert guesser.store.dated([a]) == {}
    with pytest.raises(ValueError, match="already undone"):
        geo.history.undo(batch["id"])


def test_null_island_is_never_written(fake, geo, tour):
    a = fake.add_photo("IMG_1.CR2", "2024-07-01T10:30:00")
    geo.find(berlin())
    with pytest.raises(ValueError, match="0, 0"):
        geo.place([a], 0.0, 0.0)


def day(**kw) -> GeoOptions:
    return berlin(use_tracks=False, start=datetime(2024, 7, 1, tzinfo=UTC), end=datetime(2024, 7, 2, tzinfo=UTC), **kw)


def test_nearby_speed_check(fake, geo):
    cam = fake.add_photo("IMG_0010.CR2", "2024-07-01T15:00:00")
    fake.add_photo("phone1.jpg", "2024-07-01T14:59:00", tz="Europe/Berlin", lat=48.0, lon=12.0, model="Pixel 8")
    # 100 km in 9 minutes: a flight or a wrong clock, so no point in between.
    fake.add_photo("phone2.jpg", "2024-07-01T15:08:00", tz="Europe/Berlin", lat=48.9, lon=12.0, model="Pixel 8")
    geo.find(day())
    s = geo.suggestions()[cam]
    assert s["method"] == "nearest" and s["lat"] == 48.0 and s["detail"] == "phone1.jpg"


def test_nearby_prefers_the_same_camera(fake, geo):
    cam = fake.add_photo("IMG_0010.CR2", "2024-07-01T15:00:00")
    fake.add_photo("phone.jpg", "2024-07-01T15:00:30", tz="Europe/Berlin", lat=48.0, lon=12.0, model="Pixel 8")
    fake.add_photo("IMG_0009.CR2", "2024-07-01T14:55:00", lat=47.0, lon=11.0)  # same camera, with GPS
    geo.find(day())
    s = geo.suggestions()[cam]
    assert s["detail"] == "IMG_0009.CR2" and s["same_camera"] and s["confidence"] == "medium"


def test_same_camera_is_compared_by_its_own_clock(fake, geo):
    # The camera ran an hour slow; a photo of the same camera 2 minutes earlier still counts.
    cam = fake.add_photo("IMG_0010.CR2", "2024-07-01T15:00:00")
    fake.add_photo("IMG_0009.CR2", "2024-07-01T14:58:00", lat=47.0, lon=11.0)
    geo.find(day(offset_s=3600))
    s = geo.suggestions()[cam]
    assert s["detail"] == "IMG_0009.CR2" and s["time_diff_s"] == 120 and s["confidence"] == "high"


def test_other_camera_gives_lower_confidence(fake, geo):
    cam = fake.add_photo("IMG_0010.CR2", "2024-07-01T15:00:00")
    fake.add_photo("phone.jpg", "2024-07-01T15:01:00", tz="Europe/Berlin", lat=48.0, lon=12.0, model="Pixel 8")
    geo.find(day())
    s = geo.suggestions()[cam]
    assert not s["same_camera"] and s["confidence"] == "medium"


def test_no_location_needed(fake, geo):
    shot = fake.add_photo("Screenshot_20240701-150000.png", "2024-07-01T15:00:00", make="", model="")
    photo = fake.add_photo("IMG_1.CR2", "2024-07-01T15:00:00")
    geo.find(day())
    s = geo.suggestions()
    assert s[shot].get("screenshot") and not s[photo].get("screenshot")
    assert geo.mark_no_location([shot]) == 1
    assert shot not in geo.suggestions()
    run = geo.find(day())
    assert shot not in geo.suggestions() and run["no_location_needed"] == 1
    assert [x["file_name"] for x in geo.no_location()] == ["Screenshot_20240701-150000.png"]
    geo.unmark_no_location([shot])
    geo.find(day())
    assert shot in geo.suggestions()


def test_whole_album_without_tracks(fake, geo):
    a = fake.add_photo("old.jpg", "1999-05-01T12:00:00", album="scans")
    b = fake.add_photo("older.jpg", "1987-05-01T12:00:00", album="scans")
    fake.add_photo("elsewhere.jpg", "1999-05-01T12:00:00")
    run = geo.find(berlin(use_tracks=False, album_id="scans"))
    assert run["whole_album"] and set(geo.suggestions()) == {a, b}
    assert {s["status"] for s in geo.suggestions().values()} == {"no_match"}
    geo.place([a, b], 48.137, 11.575, "Marienplatz, München")
    assert geo.suggestions()[a]["detail"] == "Marienplatz, München"


def test_known_date_records_history(fake, guesser):
    aid = fake.add_asset("scan.jpg", date(1987, 1, 1), [])
    from immich_metadatenmaster.dates import parse_date_spec
    guesser.apply_known_date([aid], parse_date_spec("06.1987"))
    batch = guesser.store.change_batch(guesser.store.change_batches()[0]["id"])
    assert batch["label"] == "Known date 1987-06" and batch["tag"] == "Metadatenmaster/Date/Manual"
    change = batch["changes"][0]
    assert change["file_name"] == "scan.jpg" and change["new"]["date"].startswith("1987-06-15T12:00")
    assert change["old"]["taken_at"] == "2026-09-01T10:00:00+00:00"
