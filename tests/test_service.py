from datetime import date

import pytest

from immich_metadatenmaster.dates import parse_date_spec
from immich_metadatenmaster.service import DESCRIPTION_PREFIX

from conftest import written_date


def test_known_date_keeps_order_and_tags(guesser, fake):
    ids = [fake.add_asset(f"scan{i}.jpg", date(1987, 5, 1), [], album="b") for i in range(3)]
    result = guesser.apply_known_date(ids, parse_date_spec("1987"))
    assert result["written"] == 3
    times = [written_date(fake.assets[i]) for i in ids]
    assert [t.date() for t in times] == [date(1987, 7, 2)] * 3
    assert times[0] < times[1] < times[2]
    assert all(fake.assets[i].tags == {"Metadatenmaster/Date/Manual"} for i in ids)
    assert fake.date_updates[0]["timeZone"] == "Europe/Berlin"
    dated = guesser.store.dated(ids)
    assert dated[ids[0]].source == "manual" and dated[ids[0]].precision == "year"


def test_known_date_without_order_uses_one_bulk_call(guesser, fake):
    ids = [fake.add_asset(f"scan{i}.jpg", date(1987, 5, 1), [], album="b") for i in range(3)]
    guesser.apply_known_date(ids, parse_date_spec("06.1987"), keep_order=False)
    assert len(fake.date_updates) == 1 and fake.date_updates[0]["ids"] == ids
    assert written_date(fake.assets[ids[0]]).date() == date(1987, 6, 15)


def test_old_immich_without_timezone_field(guesser, fake):
    fake.reject_timezone = True
    aid = fake.add_asset("a.jpg", date(1987, 5, 1), [])
    guesser.apply_known_date([aid], parse_date_spec("1987"))
    assert "timeZone" not in fake.date_updates[0]


def test_estimate_single_photo(guesser, fake, estimator):
    anna = fake.add_person("Anna", date(1980, 3, 1))
    bernd = fake.add_person("Bernd", date(1950, 6, 1))
    aid = fake.add_asset("scan.jpg", date(1986, 8, 1), [anna, bernd, None])
    res = guesser.estimate([aid])[aid]
    assert res["status"] == "ok"
    est = res["estimate"]
    assert est["low"] <= "1986-08-01" <= est["high"]
    assert abs(date.fromisoformat(est["median"]).year - 1986) <= 1
    assert "1 face without a name" in res["notes"]
    # Cached: a second run does not call the model again.
    calls = estimator.calls
    guesser.estimate([aid])
    assert estimator.calls == calls


def test_estimate_reports_missing_birthdays(guesser, fake):
    nobody = fake.add_person("Oma", None)
    aid = fake.add_asset("scan.jpg", date(1970, 1, 1), [nobody])
    res = guesser.estimate([aid])[aid]
    assert res["status"] == "no_estimate"
    assert "Oma has no birthday" in res["notes"]


def test_joint_estimate_shares_date_with_faceless_photos(guesser, fake):
    anna = fake.add_person("Anna", date(1980, 3, 1))
    carl = fake.add_person("Carl", date(1983, 1, 1))
    a = fake.add_asset("1.jpg", date(1988, 7, 1), [anna])
    b = fake.add_asset("2.jpg", date(1988, 7, 1), [carl])
    landscape = fake.add_asset("3.jpg", date(1988, 7, 1), [])
    single = guesser.estimate([a])[a]["estimate"]
    res = guesser.estimate([a, b, landscape], joint=True)
    joint = res[landscape]["estimate"]
    assert res[landscape]["status"] == "ok" and joint == res[a]["estimate"]
    assert joint["asset_count"] == 2
    width = lambda e: date.fromisoformat(e["high"]).toordinal() - date.fromisoformat(e["low"]).toordinal()
    assert width(joint) < width(single)


def test_small_faces_use_original(guesser, fake, estimator):
    anna = fake.add_person("Anna", date(1980, 3, 1))
    aid = fake.add_asset("group.jpg", date(1985, 3, 1), [anna], face_px=30)
    res = guesser.estimate([aid])[aid]
    assert fake.original_downloads == 1
    assert res["status"] == "ok"
    assert res["estimate"]["observations"][0]["estimated_age"] == pytest.approx(5.0, abs=0.6)


def test_apply_estimates_writes_date_description_and_tag(guesser, fake):
    anna = fake.add_person("Anna", date(1980, 3, 1))
    aid = fake.add_asset("scan.jpg", date(1986, 8, 1), [anna])
    other = fake.add_asset("none.jpg", date(1986, 8, 1), [])
    fake.assets[aid].description = f"Holiday\n{DESCRIPTION_PREFIX} old"
    guesser.estimate([aid, other])
    out = guesser.apply_estimates([aid, other])
    assert out == {"written": 1, "skipped": 1}
    assert abs(written_date(fake.assets[aid]).year - 1986) <= 1
    desc = fake.assets[aid].description.splitlines()
    assert desc[0] == "Holiday" and len(desc) == 2 and desc[1].startswith(DESCRIPTION_PREFIX)
    assert "Anna" in desc[1]
    assert fake.assets[aid].tags == {"Metadatenmaster/Date/Estimated"}
    assert guesser.store.estimates([aid]) == {}
    assert guesser.store.dated([aid])[aid].source == "estimate"


def test_calibration_learns_bias_and_skips_scans(guesser, fake, estimator):
    estimator.bias = 4.0  # the model sees everybody 4 years too old
    anna = fake.add_person("Anna", date(1990, 1, 1))
    bernd = fake.add_person("Bernd", date(1960, 1, 1))
    for year in range(2003, 2024):
        fake.add_asset(f"cam{year}.jpg", date(year, 6, 1), [anna, bernd], exif=True, make="Canon", model="EOS 5D")
    # A scan whose EXIF date is the scan date must not be used as ground truth.
    scan = fake.add_asset("scan.jpg", date(1995, 6, 1), [anna], exif=True, make="EPSON", model="Perfection V600")
    fake.assets[scan].exif_date = "2026-01-01T12:00:00.000Z"

    before = guesser.estimate([scan])[scan]["estimate"]
    report = guesser.calibrate(per_person=50)
    assert report.samples == 42
    assert report.mae_after < 1.5 < report.mae_before
    assert guesser.calibration_path.exists()

    after = guesser.estimate([scan])[scan]["estimate"]
    assert abs(date.fromisoformat(after["median"]).year - 1995) <= 1
    assert date.fromisoformat(before["median"]).year >= 1998  # uncorrected: ~4 years too late


def test_manually_dated_scans_count_for_calibration(guesser, fake):
    anna = fake.add_person("Anna", date(1990, 1, 1))
    ids = [fake.add_asset(f"s{y}.jpg", date(y, 7, 2), [anna]) for y in range(1992, 2000)]
    for aid, y in zip(ids, range(1992, 2000)):
        guesser.apply_known_date([aid], parse_date_spec(str(y)))
    report = guesser.calibrate()
    assert report.samples == 8


def test_open_store_takes_over_age_guesser_data(tmp_path):
    from immich_metadatenmaster.service import open_store
    from immich_metadatenmaster.store import DatedRecord, Store

    old = Store(tmp_path / "age-guesser.sqlite")
    old.save_dated(DatedRecord("a1", "manual", "1987", "year", date(1987, 1, 1), date(1987, 12, 31), date(1987, 7, 2)))
    old.close()
    store = open_store(tmp_path)
    assert store.dated(["a1"])["a1"].label == "1987"
    assert (tmp_path / "metadatenmaster.sqlite").exists() and store.tracks() == []
