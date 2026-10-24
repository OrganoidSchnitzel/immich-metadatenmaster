from datetime import datetime, timedelta, timezone

import pytest

from immich_metadatenmaster.gpx import GpxError, Track, parse_gpx, simplify, total_distance_m, with_times

from conftest import gpx

UTC = timezone.utc


def at(hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(2024, 7, 1, hh, mm, ss, tzinfo=UTC)


def test_parse_komoot_style_gpx():
    parsed = parse_gpx(gpx([(47.0, 11.0, "2024-07-01T08:00:00Z"), (47.01, 11.0, "2024-07-01T08:10:00.000Z")],
                           name="Zugspitze"))
    assert parsed.name == "Zugspitze" and parsed.has_times
    assert parsed.points[0].time == at(8, 0) and parsed.points[1].ele == 1001


def test_parse_sorts_by_time_and_merges_segments():
    data = (b'<gpx xmlns="http://www.topografix.com/GPX/1/0"><trk><trkseg>'
            b'<trkpt lat="1" lon="1"><time>2024-07-01T09:00:00Z</time></trkpt></trkseg><trkseg>'
            b'<trkpt lat="2" lon="2"><time>2024-07-01T08:00:00Z</time></trkpt>'
            b'<trkpt lat="91" lon="2"><time>2024-07-01T08:30:00Z</time></trkpt></trkseg></trk></gpx>')
    parsed = parse_gpx(data, "walk.gpx")
    assert parsed.name == "walk"  # no name in the file: the file name
    assert [p.lat for p in parsed.points] == [2, 1]  # invalid latitude dropped


def test_parse_route_without_times():
    data = (b'<gpx xmlns="http://www.topografix.com/GPX/1/1"><rte><name>Plan</name>'
            b'<rtept lat="47" lon="11"/><rtept lat="47.01" lon="11"/></rte></gpx>')
    parsed = parse_gpx(data)
    assert parsed.name == "Plan" and not parsed.has_times and len(parsed.points) == 2


@pytest.mark.parametrize("data", [
    b"not xml", b"<kml/>", b'<gpx><trk><trkseg><trkpt lat="1" lon="1"/></trkseg></trk></gpx>',
    b'<!DOCTYPE gpx [<!ENTITY a "b">]><gpx/>',
    b"<!--" + b" " * 10_000 + b'--><!DOCTYPE gpx [<!ENTITY a "b">]><gpx/>',  # hidden behind a long comment
])
def test_parse_rejects_bad_files(data):
    with pytest.raises(GpxError):
        parse_gpx(data)


def track() -> Track:
    # North along 11°E, one point every 10 minutes from 08:00 to 10:00 UTC, with a pause 09:00-09:40.
    times = [at(8, 0), at(8, 10), at(8, 20), at(8, 30), at(8, 40), at(8, 50), at(9, 0), at(9, 40), at(9, 50), at(10, 0)]
    lats = [47.0, 47.01, 47.02, 47.03, 47.04, 47.05, 47.06, 47.16, 47.17, 47.18]
    pts = parse_gpx(gpx([(lat, 11.0, t.isoformat()) for lat, t in zip(lats, times)])).points
    return Track(pts)


def test_interpolates_between_points():
    pos = track().position_at(at(8, 35), tolerance=timedelta(minutes=10), max_gap=timedelta(minutes=15))
    # Sparse points (10 min apart): the time to the nearest real point counts as the uncertainty.
    assert pos.method == "interpolated" and pos.lat == pytest.approx(47.035) and pos.time_diff_s == 300
    assert pos.ele == pytest.approx(1003.5)


def test_tolerance_before_start_and_after_end():
    t = track()
    kw = {"tolerance": timedelta(minutes=10), "max_gap": timedelta(minutes=15)}
    assert t.position_at(at(7, 55), **kw).lat == 47.0
    assert t.position_at(at(10, 9), **kw).time_diff_s == 540
    assert t.position_at(at(7, 49), **kw) is None
    assert t.position_at(at(10, 11), **kw) is None


def test_moving_gap_is_not_interpolated():
    t = track()
    kw = {"tolerance": timedelta(minutes=10), "max_gap": timedelta(minutes=15)}
    near_start = t.position_at(at(9, 5), **kw)
    assert near_start.method == "nearest" and near_start.lat == 47.06 and near_start.time_diff_s == 300
    assert t.position_at(at(9, 20), **kw) is None  # 20 min from both ends of the pause
    # With a larger allowed gap the pause is bridged.
    assert t.position_at(at(9, 20), tolerance=timedelta(minutes=10), max_gap=timedelta(hours=1)).lat == \
        pytest.approx(47.11)


def test_stationary_pause_is_interpolated():
    pts = parse_gpx(gpx([(47.0, 11.0, "2024-07-01T08:00:00Z"), (47.0001, 11.0, "2024-07-01T10:00:00Z")])).points
    pos = Track(pts).position_at(at(9, 0), tolerance=timedelta(minutes=10), max_gap=timedelta(minutes=15))
    assert pos is not None and pos.method == "interpolated"


def test_with_times_spreads_by_distance():
    pts = parse_gpx(gpx([(47.0, 11.0, None), (47.01, 11.0, None), (47.03, 11.0, None)])).points
    timed = with_times(pts, at(8, 0), at(9, 30))
    assert [p.time for p in timed] == [at(8, 0), at(8, 30), at(9, 30)]
    assert total_distance_m(pts) == pytest.approx(3336, rel=0.01)
    with pytest.raises(GpxError):
        with_times(pts, at(9, 0), at(8, 0))


def test_simplify_keeps_ends():
    pts = parse_gpx(gpx([(47 + i / 1000, 11.0, None) for i in range(10)])).points
    line = simplify(pts, max_points=3)
    assert line[0] == [47.0, 11.0] and line[-1] == [47.009, 11.0] and len(line) <= 5
