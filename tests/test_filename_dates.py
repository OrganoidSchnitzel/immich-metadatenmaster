from datetime import date, datetime, time

import pytest

from immich_metadatenmaster.filename_dates import date_from_name, differs


@pytest.mark.parametrize("name, day, t", [
    ("IMG_20190713_142233.jpg", date(2019, 7, 13), time(14, 22, 33)),
    ("PXL_20230101_123456789.jpg", date(2023, 1, 1), time(12, 34, 56)),
    ("IMG-20190713-WA0001.jpg", date(2019, 7, 13), None),
    ("WhatsApp Image 2020-01-01 at 12.00.00.jpeg", date(2020, 1, 1), time(12, 0)),
    ("Screenshot_20200101-120000.png", date(2020, 1, 1), time(12, 0)),
    ("signal-2021-01-01-120000.jpg", date(2021, 1, 1), time(12, 0)),
    ("2019-07-13 14.22.33.jpg", date(2019, 7, 13), time(14, 22, 33)),
    ("1987-07-01 Holiday.jpg", date(1987, 7, 1), None),
])
def test_dates_in_names(name, day, t):
    nd = date_from_name(name, today=date(2026, 1, 1))
    assert nd is not None and (nd.day, nd.time) == (day, t)


@pytest.mark.parametrize("name", ["DSC_1234.JPG", "20190713123.jpg", "2019-13-01.jpg", "VID_20991231_000000.mp4",
                                  "P1012345.JPG"])
def test_no_date(name):
    assert date_from_name(name, today=date(2026, 1, 1)) is None


def test_differs():
    nd = date_from_name("IMG_20190713_142233.jpg")
    assert not differs(nd, datetime(2019, 7, 13, 14, 23))
    assert differs(nd, datetime(2019, 7, 13, 18, 0)) and differs(nd, None)
    day_only = date_from_name("IMG-20190713-WA0001.jpg")
    assert not differs(day_only, datetime(2019, 7, 13, 23, 0)) and differs(day_only, datetime(2019, 7, 14, 1, 0))


def test_name_dates_flow(fake, guesser):
    wa1 = fake.add_asset("IMG-20190713-WA0001.jpg", date(2019, 7, 13), [])
    wa2 = fake.add_asset("IMG-20190713-WA0002.jpg", date(2019, 7, 13), [])
    ok = fake.add_asset("IMG_20260901_100000.jpg", date(2026, 9, 1), [])  # Immich already has this date
    plain = fake.add_asset("DSC_0001.jpg", date(2026, 9, 1), [])
    assets = [guesser.immich.asset(i) for i in (wa1, wa2, ok, plain)]

    assert guesser.suggest_name_dates(assets) == {"checked": 4, "with_date": 3, "suggested": 2}
    assert set(guesser.store.suggestions("name_date")) == {wa1, wa2}
    assert fake.date_updates == []

    result = guesser.apply_name_dates([wa1, wa2, plain])
    assert result == {"written": 2, "skipped": 1}
    assert fake.assets[wa1].exif_date == "2019-07-13T12:00:00.000+02:00"
    assert fake.assets[wa2].exif_date == "2019-07-13T12:01:00.000+02:00"  # keeps the order within the day
    assert fake.assets[wa1].tags == {"Metadatenmaster/Date/File name"}
    assert guesser.store.dated([wa1])[wa1].source == "filename"
    assert guesser.store.suggestions("name_date") == {}
