"""Error model of the age estimator: bias and spread as a function of the true age.

Without calibration a generic prior is used. `fit_calibration` learns the model from photos in the
library whose date is known, so the estimator's quirks (and each person's) are corrected.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

AGE_KNOTS = [0, 2, 4, 7, 10, 13, 16, 20, 25, 30, 40, 50, 60, 70, 80, 95]

# Pseudo-counts that pull sparse bins towards the prior.
_GLOBAL_PRIOR_WEIGHT = 3.0
_BIN_PRIOR_WEIGHT = 8.0
_PERSON_PRIOR_WEIGHT = 5.0


def default_sigma(age: np.ndarray | float, scale: float = 1.0) -> np.ndarray:
    """Typical standard deviation of an age estimate: ~1 year for toddlers, ~4 at 30, ~8 at 70."""
    return scale * (0.7 + 0.11 * np.asarray(age, dtype=float))


@dataclass
class Calibration:
    knots: list[float]
    bias: list[float]
    sigma: list[float]
    person_bias: dict[str, float] = field(default_factory=dict)
    samples: int = 0
    backend: str = ""
    created_at: str = ""

    @classmethod
    def default(cls, sigma_scale: float = 1.0, backend: str = "") -> "Calibration":
        knots = [float(k) for k in AGE_KNOTS]
        return cls(knots, [0.0] * len(knots), default_sigma(np.array(knots), sigma_scale).tolist(),
                   backend=backend)

    @property
    def is_default(self) -> bool:
        return self.samples == 0

    def bias_at(self, true_age: np.ndarray, person_id: str | None = None) -> np.ndarray:
        b = np.interp(true_age, self.knots, self.bias)
        return b + self.person_bias.get(person_id or "", 0.0)

    def sigma_at(self, true_age: np.ndarray) -> np.ndarray:
        return np.interp(true_age, self.knots, self.sigma)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2))

    @classmethod
    def load(cls, path: Path) -> "Calibration":
        return cls(**json.loads(path.read_text()))


@dataclass(frozen=True)
class Sample:
    person_id: str
    true_age: float
    estimated_age: float


@dataclass
class CalibrationReport:
    samples: int
    people: int
    mae_before: float
    mae_after: float
    bins: list[dict[str, float]]


def _bin_weights(ages: np.ndarray, knots: list[float]) -> np.ndarray:
    """Triangular (linear interpolation) weights of each sample for each knot: shape (samples, knots)."""
    k = np.asarray(knots, dtype=float)
    w = np.zeros((len(ages), len(k)))
    for j in range(len(k)):
        left = k[j - 1] if j > 0 else -np.inf
        right = k[j + 1] if j < len(k) - 1 else np.inf
        rising = (ages >= left) & (ages <= k[j])
        falling = (ages > k[j]) & (ages <= right)
        if np.isfinite(left):
            w[rising, j] = (ages[rising] - left) / (k[j] - left)
        else:
            w[rising, j] = 1.0
        if np.isfinite(right):
            w[falling, j] = (right - ages[falling]) / (right - k[j])
        else:
            w[falling, j] = 1.0
    return w


def fit_calibration(samples: list[Sample], *, sigma_scale: float = 1.0, backend: str = "") -> tuple[Calibration, CalibrationReport]:
    prior = Calibration.default(sigma_scale, backend)
    if not samples:
        raise ValueError("No calibration samples: tag people, set their birthdays and keep some dated photos.")

    true_age = np.array([s.true_age for s in samples])
    est = np.array([s.estimated_age for s in samples])
    resid = est - true_age
    prior_sigma = default_sigma(true_age, sigma_scale)
    # Clip gross errors (wrong face tags, wrong dates) so they cannot dominate the fit.
    clipped = np.clip(resid, -3 * prior_sigma, 3 * prior_sigma)

    w = _bin_weights(true_age, prior.knots)
    wsum = w.sum(axis=0)
    person_ids = np.array([s.person_id for s in samples])
    people = sorted(set(person_ids.tolist()))
    masks = {pid: person_ids == pid for pid in people}

    # Backfitting: alternate between the per-age bias and each person's offset.
    person_offset = np.zeros(len(samples))
    person_bias: dict[str, float] = {}
    for _ in range(10):
        # A global offset (e.g. the model sees everybody on old scans as older) is learned from all
        # samples; the age bins only add deviations from it, so sparse bins fall back to the global value.
        r = clipped - person_offset
        global_bias = r.sum() / (len(r) + _GLOBAL_PRIOR_WEIGHT)
        bias = global_bias + (w * (r - global_bias)[:, None]).sum(axis=0) / (wsum + _BIN_PRIOR_WEIGHT)
        after_bin = clipped - np.interp(true_age, prior.knots, bias)
        person_bias = {}
        person_offset = np.zeros(len(samples))
        for pid, mask in masks.items():
            if mask.sum() >= 3:
                person_bias[pid] = float(after_bin[mask].sum() / (mask.sum() + _PERSON_PRIOR_WEIGHT))
        # Person offsets are deviations from the average person; the age bins carry the common part.
        centre = sum(person_bias[pid] * masks[pid].sum() for pid in person_bias) / max(
            1, sum(masks[pid].sum() for pid in person_bias))
        for pid in person_bias:
            person_bias[pid] = float(person_bias[pid] - centre)
            person_offset[masks[pid]] = person_bias[pid]

    centred = clipped - np.interp(true_age, prior.knots, bias) - person_offset
    prior_var = np.asarray(prior.sigma) ** 2
    var = ((w * centred[:, None] ** 2).sum(axis=0) + _BIN_PRIOR_WEIGHT * prior_var) / (wsum + _BIN_PRIOR_WEIGHT)
    sigma = np.sqrt(var)

    cal = Calibration(prior.knots, bias.round(3).tolist(), sigma.round(3).tolist(),
                      person_bias={k: round(v, 3) for k, v in person_bias.items()}, samples=len(samples),
                      backend=backend, created_at=datetime.now().isoformat(timespec="seconds"))

    corrected = est - np.array([cal.bias_at(np.array(s.true_age), s.person_id) for s in samples]).ravel()
    bins = [
        {"age": k, "samples": round(float(n), 1), "bias": b, "sigma": sd}
        for k, n, b, sd in zip(cal.knots, wsum, cal.bias, cal.sigma)
    ]
    report = CalibrationReport(
        samples=len(samples),
        people=len(people),
        mae_before=round(float(np.abs(resid).mean()), 2),
        mae_after=round(float(np.abs(corrected - true_age).mean()), 2),
        bins=bins,
    )
    return cal, report
