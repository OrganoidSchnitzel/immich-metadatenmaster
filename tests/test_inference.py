from datetime import date

import pytest

from immich_metadatenmaster.calibration import Calibration
from immich_metadatenmaster.inference import AgeObservation, EstimationError, estimate_date

CAL = Calibration.default()


def obs(name, birth, age, pid=None):
    return AgeObservation(pid or name, name, birth, age)


def test_child_gives_tight_estimate():
    est = estimate_date([obs("Anna", date(1980, 3, 1), 5.0)], CAL)
    assert abs(est.median.year + est.median.month / 12 - 1985.2) < 0.4
    assert est.width_years < 5
    assert est.low < est.median < est.high


def test_adult_gives_wide_estimate():
    est = estimate_date([obs("Bernd", date(1950, 1, 1), 40.0)], CAL)
    assert 1987 <= est.median.year <= 1992
    assert est.width_years > 10
    assert est.confidence == "low"


def test_more_faces_narrow_the_estimate():
    one = estimate_date([obs("Bernd", date(1950, 1, 1), 40.0)], CAL)
    both = estimate_date([obs("Bernd", date(1950, 1, 1), 40.0), obs("Anna", date(1982, 6, 1), 8.0)], CAL)
    assert both.width_years < one.width_years / 2
    assert 1989 <= both.median.year <= 1991


def test_mistagged_face_is_flagged_and_outvoted():
    good = [obs("Anna", date(1982, 6, 1), 8.0), obs("Carl", date(1984, 1, 1), 6.3)]
    # Grandchild born 2015 mis-tagged in a ~1990 photo: impossible, should not drag the result.
    bad = obs("Emil", date(2015, 1, 1), 5.0)
    est = estimate_date(good + [bad], CAL)
    assert 1989 <= est.median.year <= 1991
    assert bad.outlier and not any(o.outlier for o in good)


def test_bounds_constrain():
    est = estimate_date([obs("Bernd", date(1950, 1, 1), 40.0)], CAL,
                        not_before=date(1985, 1, 1), not_after=date(1987, 12, 31))
    assert date(1985, 1, 1) <= est.low <= est.high <= date(1987, 12, 31)


def test_person_bias_shifts_estimate():
    cal = Calibration.default()
    cal.person_bias = {"anna": 2.0}  # the model sees Anna 2 years older than she is
    plain = estimate_date([obs("Anna", date(1980, 1, 1), 10.0, "anna")], CAL)
    corrected = estimate_date([obs("Anna", date(1980, 1, 1), 10.0, "anna")], cal)
    assert (plain.median - corrected.median).days == pytest.approx(730, abs=60)


def test_errors():
    with pytest.raises(EstimationError):
        estimate_date([], CAL)
    with pytest.raises(EstimationError):
        estimate_date([obs("Anna", date(1980, 1, 1), 5)], CAL, not_after=date(1970, 1, 1))


def test_roundtrip():
    est = estimate_date([obs("Anna", date(1980, 3, 1), 5.0)], CAL)
    from immich_metadatenmaster.inference import DateEstimate
    again = DateEstimate.from_dict(est.to_dict())
    assert again.median == est.median and again.observations[0].person_name == "Anna"


def test_same_person_twice_is_not_double_evidence():
    once = estimate_date([obs("Anna", date(1980, 3, 1), 7.0)], CAL)
    twice = estimate_date([obs("Anna", date(1980, 3, 1), 7.0), obs("Anna", date(1980, 3, 1), 7.0)], CAL)
    assert twice.width_years == pytest.approx(once.width_years, abs=0.1)
