from datetime import date

import pytest

from immich_metadatenmaster.dates import parse_date_spec, parse_year_bound, timestamps


@pytest.mark.parametrize("text, start, end, precision", [
    ("1987", date(1987, 1, 1), date(1987, 12, 31), "year"),
    ("ca. 1987", date(1987, 1, 1), date(1987, 12, 31), "year"),
    ("1987-06", date(1987, 6, 1), date(1987, 6, 30), "month"),
    ("06.1987", date(1987, 6, 1), date(1987, 6, 30), "month"),
    ("2/1988", date(1988, 2, 1), date(1988, 2, 29), "month"),
    ("1987-06-14", date(1987, 6, 14), date(1987, 6, 14), "day"),
    ("14.6.1987", date(1987, 6, 14), date(1987, 6, 14), "day"),
    ("1985-1989", date(1985, 1, 1), date(1989, 12, 31), "range"),
    ("1985 – 1989", date(1985, 1, 1), date(1989, 12, 31), "range"),
    ("1980s", date(1980, 1, 1), date(1989, 12, 31), "range"),
    ("80er", date(1980, 1, 1), date(1989, 12, 31), "range"),
])
def test_parse(text, start, end, precision):
    spec = parse_date_spec(text)
    assert (spec.start, spec.end, spec.precision) == (start, end, precision)


@pytest.mark.parametrize("text", ["", "hello", "1987-13", "31.02.1987", "1700", "3000", "1989-1985", "1985s"])
def test_parse_rejects(text):
    with pytest.raises(ValueError):
        parse_date_spec(text)


def test_anchor():
    assert parse_date_spec("1987").anchor("middle") == date(1987, 7, 2)
    assert parse_date_spec("1987").anchor("start") == date(1987, 1, 1)
    assert parse_date_spec("06.1987").anchor("middle") == date(1987, 6, 15)
    assert parse_date_spec("14.06.1987").anchor("middle") == date(1987, 6, 14)


def test_bounds():
    assert parse_year_bound("1970", end=False) == date(1970, 1, 1)
    assert parse_year_bound("1970", end=True) == date(1970, 12, 31)
    assert parse_year_bound("", end=True) is None


def test_timestamps_keep_order_and_timezone():
    ts = timestamps(date(1987, 7, 2), 3, "Europe/Berlin")
    assert ts == ["1987-07-02T12:00:00.000+02:00", "1987-07-02T12:01:00.000+02:00", "1987-07-02T12:02:00.000+02:00"]
    assert timestamps(date(1987, 1, 2), 1, "Europe/Berlin")[0].endswith("+01:00")

