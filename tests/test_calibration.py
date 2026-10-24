import numpy as np
import pytest

from immich_metadatenmaster.calibration import Calibration, Sample, fit_calibration


def test_fit_recovers_bias_and_person_offsets(tmp_path):
    rng = np.random.default_rng(1)
    samples = []
    for pid, offset in [("a", 0.0), ("b", 3.0), ("c", 0.0)]:
        for age in rng.uniform(1, 70, 400):
            # The model overestimates adults by 2 years, plus person b looks 3 years older.
            bias = 2.0 if age > 25 else 0.0
            est = age + bias + offset + rng.normal(0, 0.04 * age + 0.3)
            samples.append(Sample(pid, float(age), float(est)))
    cal, report = fit_calibration(samples, backend="test")
    assert float(cal.bias_at(np.array(45.0), "a")) == pytest.approx(2.0, abs=0.6)
    assert float(cal.bias_at(np.array(10.0), "a")) == pytest.approx(0.0, abs=0.6)
    assert cal.person_bias["b"] - cal.person_bias["a"] == pytest.approx(3.0, abs=0.6)
    assert report.mae_after < report.mae_before
    cal.save(tmp_path / "c.json")
    assert Calibration.load(tmp_path / "c.json") == cal


def test_needs_samples():
    with pytest.raises(ValueError):
        fit_calibration([])


def test_default_sigma_grows_with_age():
    cal = Calibration.default()
    assert cal.sigma_at(np.array(2.0)) < 1.2 < cal.sigma_at(np.array(40.0))
