from datetime import date, datetime, timezone

import pytest

from immich_metadatenmaster.geotag import GeoOptions, lacks_zone, photo_instant, place_zone, sibling_key

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
    same = ["IMG_1234.CR2", "IMG_1234-HDR.dng", "img_1234_edited.jpg", "IMG_1234-HDR-2.dng", "IMG_1234 (1).jpg"]
    assert {sibling_key(n) for n in same} == {"img_1234"}
    assert sibling_key("IMG_1234.CR2") != sibling_key("IMG_1235.CR2")
    # A series of differently named photos are not versions of one photo.
    assert sibling_key("Urlaub 1.jpg") != sibling_key("Urlaub 2.jpg")
    assert sibling_key("DSC_12.JPG") != sibling_key("DSC_47.JPG")


def test_sibling_compares_camera_clock_time(fake, geo):
    # The JPEG export stores a time zone, the RAW does not: Immich keeps instants 2 h apart, but both
    # show the same camera time, so they are the same shot.
    fake.add_photo("IMG_1234.JPG", "2024-07-01T15:00:00", tz="Europe/Berlin", lat=46.5, lon=10.5)
    raw = fake.add_photo("IMG_1234.CR2", "2024-07-01T15:00:00")
    # Same name, but taken hours later (the camera counter wrapped around): not the same shot.
    later = fake.add_photo("IMG_1234-1.CR2", "2024-07-01T19:00:00")
    geo.find(berlin(use_tracks=False, use_nearby=False,
                    start=datetime(2024, 7, 1, tzinfo=UTC), end=datetime(2024, 7, 2, tzinfo=UTC)))
    s = geo.suggestions()
    assert s[raw]["source"] == "sibling" and s[raw]["detail"] == "IMG_1234.JPG"
    assert s[later]["status"] == "no_match"


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


def test_hidden_live_photo_videos_are_left_out(fake, geo, tour):
    photo = fake.add_photo("IMG_0001.HEIC", "2024-07-01T10:35:00")
    video = fake.add_photo("IMG_0001.MOV", "2024-07-01T10:35:00")
    fake.assets[video].type, fake.assets[video].visibility = "VIDEO", "hidden"
    archived = fake.add_photo("IMG_0002.HEIC", "2024-07-01T10:40:00")
    fake.assets[archived].visibility = "archive"
    geo.find(berlin())
    assert set(geo.suggestions()) == {photo, archived}


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
    assert result == {"written": 1, "skipped": 0, "times_fixed": 1, "local_times": 0}
    assert (fake.assets[a].latitude, fake.assets[a].longitude) == (pytest.approx(47.035), 11.0)
    assert fake.assets[a].exif_date == "2024-07-01T10:35:00.000+02:00"
    assert fake.assets[a].tags == {"Metadatenmaster/Location/GPX"}
    assert geo.suggestions() == {}

    batches = geo.store.change_batches()
    assert [(b["kind"], b["source"]) for b in batches] == [("date", "clock"), ("location", "gpx")]



def test_local_time_of_the_place(fake, geo, guesser):
    """A camera still on German summer time in Colorado: its 21:45 was 13:45 there."""
    cam = fake.add_photo("IMG_7206.JPG", "2022-08-25T21:45:44", make="Canon", model="Canon EOS 77D")
    slow = fake.add_photo("IMG_7207.JPG", "2022-08-25T21:20:00", make="Canon", model="Canon EOS 6D")
    phone = fake.add_photo("PXL_20220825_194600.jpg", "2022-08-25T13:46:00", tz="America/Denver",
                           lat=40.3055, lon=-105.69113, make="Google", model="Pixel 6")
    geo.find(berlin(use_tracks=False, start=datetime(2022, 8, 25, tzinfo=UTC), end=datetime(2022, 8, 26, tzinfo=UTC),
                    nearby_min=30, camera_offsets={"Canon EOS 6D": 1800}))  # the 6D ran 30 minutes slow
    s = geo.suggestions()
    assert s[cam]["source"] == "nearby" and s[slow]["source"] == "nearby"

    result = geo.apply([cam, slow], fix_time=True, local_time=True)
    assert result == {"written": 2, "skipped": 0, "times_fixed": 0, "local_times": 2}
    assert (fake.assets[cam].exif_date, fake.assets[cam].time_zone) == ("2022-08-25T13:45:44.000-06:00",
                                                                        "America/Denver")
    assert fake.assets[slow].exif_date == "2022-08-25T13:50:00.000-06:00"  # with the clock correction
    assert fake.assets[phone].exif_date == "2022-08-25T13:46:00.000-06:00"  # has its own zone: untouched
    batch = geo.store.change_batches()[0]
    assert (batch["kind"], batch["source"], batch["label"]) == ("date", "localtime", "Local time of the place")

    guesser.history.undo(batch["id"])
    # Back to 21:45 without a real zone (Immich takes "+00:00" as UTC, which is what it assumed before).
    assert (fake.assets[cam].exif_date, fake.assets[cam].time_zone) == ("2022-08-25T21:45:44.000+00:00", "UTC")


def test_place_zone():
    assert place_zone(40.3055, -105.69113) == "America/Denver" and place_zone(47.4211, 10.9853) == "Europe/Berlin"
    assert all(lacks_zone(z) for z in (None, "", "UTC", "UTC+0", "Etc/UTC", "nonsense"))
    assert not any(lacks_zone(z) for z in ("Europe/Berlin", "UTC+2", "America/Denver"))

def test_time_zone_hint(fake, geo, tour):
    for i, local in enumerate(["10:02", "10:30", "11:55"]):
        fake.add_photo(f"IMG_{i}.CR2", f"2024-07-01T{local}:00")
    run = geo.find(GeoOptions(camera_tz="UTC"))  # wrong: the camera showed Berlin time
    assert run["hints"] == [{"camera": "Canon EOS 80D", "hours": -2, "matches": 3, "current": 1}]
    assert geo.find(berlin())["hints"] == []


def test_offset_per_camera(fake, geo, tour):
    # The Canon ran 30 min slow, the GoPro 2 h fast (still on another time zone).
    canon = fake.add_photo("IMG_0001.CR2", "2024-07-01T10:05:00")
    gopro = fake.add_photo("GOPR0001.JPG", "2024-07-01T12:35:00", make="GoPro", model="HERO9 Black")
    run = geo.find(berlin(camera_offsets={"Canon EOS 80D": 1800, "GoPro HERO9 Black": -7200}))
    s = geo.suggestions()
    assert s[canon]["lat"] == pytest.approx(47.035) and s[canon]["offset_s"] == 1800
    assert s[gopro]["lat"] == pytest.approx(47.035) and s[gopro]["corrected"] == "2024-07-01T10:35:00.000+02:00"
    assert {c["camera"]: (c["in_scope"], c["suggested"], c["offset_s"]) for c in run["cameras"]} == {
        "Canon EOS 80D": (1, 1, 1800), "GoPro HERO9 Black": (1, 1, -7200)}
    geo.apply([canon, gopro], fix_time=True)
    assert geo.store.change_batches()[0]["label"] == "Clock corrected (2 different offsets)"


def test_camera_stats_include_photos_outside_the_time_span(fake, geo, tour):
    # All photos of this camera are outside the track because its clock is 3 h off: the camera is still
    # listed, with photos to work out the offset from.
    for i in range(3):
        fake.add_photo(f"DSC_{i}.JPG", f"2024-07-01T{13 + i // 2}:{i * 10:02d}:00", make="Sony", model="ILCE-7M3")
    run = geo.find(berlin())
    sony = next(c for c in run["cameras"] if c["camera"] == "Sony ILCE-7M3")
    assert sony["without_location"] == 3 and sony["in_scope"] == 0 and len(sony["photos"]) == 3


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



def test_located_photos_far_off_the_track_get_its_position(fake, geo, guesser, tour):
    # Earlier the whole day was put at one point on the map; the track knows better.
    wrong = fake.add_photo("IMG_1.CR2", "2024-07-01T10:30:00", lat=47.5, lon=11.5)
    close = fake.add_photo("IMG_2.CR2", "2024-07-01T10:30:00", lat=47.0305, lon=11.0)  # about 55 m off: fine
    later = fake.add_photo("IMG_3.CR2", "2024-07-01T15:00:00", lat=47.5, lon=11.5)  # not on the track
    phone = fake.add_photo("PXL_1.jpg", "2024-07-01T10:30:00", tz="Europe/Berlin", lat=47.51, lon=11.52,
                           make="Google", model="Pixel 8")  # a phone's own GPS is trusted
    run = geo.find(berlin(start=datetime(2024, 7, 1, 6, tzinfo=UTC), end=datetime(2024, 7, 1, 18, tzinfo=UTC)))
    assert run["relocated"] == 0 and geo.suggestions() == {}  # only on request

    run = geo.find(berlin(start=datetime(2024, 7, 1, 6, tzinfo=UTC), end=datetime(2024, 7, 1, 18, tzinfo=UTC),
                          include_located=True))
    s = geo.suggestions()
    assert run["relocated"] == 1 and run["located_listed"] == 4
    assert (s[wrong]["status"], s[wrong]["source"], s[wrong]["current_lat"]) == ("ok", "gpx", 47.5)
    assert s[wrong]["lat"] == pytest.approx(47.03)
    assert s[close]["status"] == s[later]["status"] == s[phone]["status"] == "located"

    assert geo.apply(list(s))["written"] == 1
    assert (fake.assets[wrong].latitude, fake.assets[wrong].longitude) == (pytest.approx(47.03), 11.0)
    guesser.history.undo(geo.store.change_batches()[0]["id"])
    assert (fake.assets[wrong].latitude, fake.assets[wrong].longitude) == (47.5, 11.5)


def test_phone_photos_placed_together_by_hand_get_track_positions(fake, geo, tour):
    # A phone without GPS: all its photos of the day were put at one point on the map, hours apart.
    placed = [fake.add_photo(f"IMG2024070{i}.jpg", f"2024-07-01T{t}:00", tz="Europe/Berlin", lat=39.6, lon=2.9,
                             make="realme", model="GT 2") for i, t in enumerate(["10:10", "10:40", "11:50"])]
    own_gps = fake.add_photo("PXL_1.jpg", "2024-07-01T10:30:00", tz="Europe/Berlin", lat=47.5, lon=11.5,
                             make="Google", model="Pixel 8")
    later = fake.add_photo("IMG_9.CR2", "2024-07-01T15:00:00", lat=47.2, lon=11.2)
    run = geo.find(berlin(start=datetime(2024, 7, 1, 6, tzinfo=UTC), end=datetime(2024, 7, 1, 18, tzinfo=UTC),
                          include_located=True))
    s = geo.suggestions()
    assert run["relocated"] == 3 and all(s[a]["status"] == "ok" and s[a]["source"] == "gpx" for a in placed)
    assert s[own_gps]["keep_reason"] == "probably the phone's own GPS, which is kept"
    assert s[later]["keep_reason"] == "outside the tracks' time span (nearest track: 3 h 00 min earlier)"
    stats = {c["camera"]: c for c in run["cameras"]}
    assert (stats["realme GT 2"]["with_location"], stats["realme GT 2"]["moved"]) == (3, 3)
    assert stats["Canon EOS 80D"]["kept"] == {"outside the tracks' time span": 1}


def test_photos_placed_by_hand_may_use_a_planned_route(fake, geo):
    route = geo.add_track("route.gpx", gpx([(47.0 + i * 0.01, 11.0, None) for i in range(13)], name="Planned"))
    geo.set_track_times(route["id"], datetime(2024, 7, 1, 8, tzinfo=UTC), datetime(2024, 7, 1, 10, tzinfo=UTC))
    placed = [fake.add_photo(f"IMG_{i}.CR2", f"2024-07-01T{t}:00", lat=39.6, lon=2.9)
              for i, t in enumerate(["10:10", "10:40", "11:50"])]
    single = fake.add_photo("IMG_9.CR2", "2024-07-01T10:30:00", lat=47.5, lon=11.5)
    run = geo.find(berlin(start=datetime(2024, 7, 1, 6, tzinfo=UTC), end=datetime(2024, 7, 1, 18, tzinfo=UTC),
                          include_located=True))
    s = geo.suggestions()
    assert run["relocated"] == 3 and all(s[a]["status"] == "ok" for a in placed)
    assert s[single]["keep_reason"].startswith("no recorded GPX track")

def test_album_filter(fake, geo, tour):
    inside = fake.add_photo("IMG_1.CR2", "2024-07-01T10:30:00", album="trip")
    fake.add_photo("IMG_2.CR2", "2024-07-01T10:40:00")
    geo.find(berlin(album_id="trip"))
    assert set(geo.suggestions()) == {inside}


def day(**kw) -> GeoOptions:
    return berlin(use_tracks=False, start=datetime(2024, 7, 1, tzinfo=UTC), end=datetime(2024, 7, 2, tzinfo=UTC), **kw)


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


def test_undo_of_several_locations(fake, geo, guesser, tour):
    ids = [fake.add_photo(f"IMG_{i}.CR2", f"2024-07-01T10:{30 + i}:00") for i in range(3)]
    geo.find(berlin())
    geo.apply(ids)
    assert geo.history.undo(last_batch(guesser.store)["id"]) == {**UNDO, "restored": 3}
    assert all(fake.assets[i].latitude is None for i in ids)


def test_write_failing_part_way(fake, geo, guesser, tour):
    ids = [fake.add_photo(f"IMG_{i}.CR2", f"2024-07-01T10:{30 + i}:00") for i in range(3)]
    geo.find(berlin())
    fake.fail_location_after = 1
    with pytest.raises(Exception, match="500"):
        geo.apply(ids)
    # The photo that was written is logged, tagged and no longer pending; the others still are.
    batch = last_batch(guesser.store)
    assert [c["asset_id"] for c in batch["changes"]] == [ids[0]]
    assert fake.assets[ids[0]].tags == {"Metadatenmaster/Location/GPX"}
    assert set(geo.suggestions()) == {ids[1], ids[2]}


def test_no_time_correction_for_photos_that_have_a_location(fake, geo):
    a = fake.add_photo("IMG_1.CR2", "2024-07-01T10:30:00", lat=46.0, lon=9.0)
    b = fake.add_photo("IMG_2.CR2", "2024-07-01T10:31:00")
    geo.find(day(include_located=True, offset_s=600))
    s = geo.suggestions()
    assert "corrected" not in s[a] and "corrected" in s[b]


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


def test_clock_candidates(fake, geo):
    cam = fake.add_photo("IMG_0001.CR2", "2024-07-01T10:05:00")
    phone = fake.add_photo("PXL_1.jpg", "2024-07-01T10:35:00", tz="Europe/Berlin", make="Google", model="Pixel 8")
    fake.add_photo("PXL_2.jpg", "2024-07-01T13:00:00", tz="Europe/Berlin", make="Google", model="Pixel 8")
    fake.add_photo("PXL_far.jpg", "2024-07-02T10:00:00", tz="Europe/Berlin", make="Google", model="Pixel 8")
    fake.add_photo("IMG_0002.CR2", "2024-07-01T10:06:00")  # same camera: not a reference
    r = geo.clock_candidates(cam, camera_tz="Europe/Berlin")
    assert r["photo"]["file_name"] == "IMG_0001.CR2"
    # In time order (the user scans them for the same moment).
    assert [(o["file_name"], o["delta_s"]) for o in r["others"]] == [("PXL_1.jpg", 1800), ("PXL_2.jpg", 10500)]
    assert r["others"][0]["id"] == phone and r["others"][0]["has_time_zone"]
