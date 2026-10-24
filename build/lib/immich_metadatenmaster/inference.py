"""Turn estimated ages of known people into a probability distribution over the photo date.

For every candidate date t the true age of person i is (t - birth_i). The estimator's output is
modelled as  estimated_age ~ Normal(true_age + bias(true_age), sigma(true_age)),  mixed with a small
uniform "outlier" component so that one wrong face tag or a badly estimated face cannot veto the
others. Multiplying the likelihoods of all faces (possibly from several photos of the same event)
gives the posterior over t, from which the median and a 90% interval are read.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

import numpy as np

from .calibration import Calibration
from .dates import MIN_YEAR

OUTLIER_WEIGHT = 0.04
_OUTLIER_DENSITY = 1 / 100  # uniform over 0-100 years
_DAYS_PER_YEAR = 365.2425


class EstimationError(ValueError):
    pass


@dataclass
class AgeObservation:
    person_id: str
    person_name: str
    birth_date: date
    estimated_age: float
    asset_id: str = ""
    face_id: str = ""
    # Filled in by estimate_date:
    age_at_estimate: float | None = None
    outlier: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["birth_date"] = self.birth_date.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgeObservation":
        return cls(**{**d, "birth_date": date.fromisoformat(d["birth_date"])})


@dataclass
class DateEstimate:
    median: date
    low: date  # 5% quantile
    high: date  # 95% quantile
    mode: date
    observations: list[AgeObservation] = field(default_factory=list)
    asset_count: int = 1

    @property
    def width_years(self) -> float:
        return (self.high.toordinal() - self.low.toordinal()) / _DAYS_PER_YEAR

    @property
    def confidence(self) -> str:
        w = self.width_years
        return "high" if w <= 2 else "medium" if w <= 6 else "low"

    @property
    def label(self) -> str:
        if self.width_years <= 1.5:
            return self.median.strftime("%b %Y")
        return str(self.median.year)

    @property
    def range_label(self) -> str:
        if self.low.year == self.high.year:
            return f"{self.low:%b}–{self.high:%b %Y}"
        return f"{self.low.year}–{self.high.year}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "median": self.median.isoformat(),
            "low": self.low.isoformat(),
            "high": self.high.isoformat(),
            "mode": self.mode.isoformat(),
            "asset_count": self.asset_count,
            "observations": [o.to_dict() for o in self.observations],
            "label": self.label,
            "range_label": self.range_label,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DateEstimate":
        return cls(
            median=date.fromisoformat(d["median"]),
            low=date.fromisoformat(d["low"]),
            high=date.fromisoformat(d["high"]),
            mode=date.fromisoformat(d["mode"]),
            observations=[AgeObservation.from_dict(o) for o in d.get("observations", [])],
            asset_count=d.get("asset_count", 1),
        )


def _log_likelihood(obs: AgeObservation, ordinals: np.ndarray, cal: Calibration) -> tuple[np.ndarray, np.ndarray]:
    """Log-likelihood of one observation for each grid date, and the Gaussian (inlier) part alone."""
    true_age = (ordinals - obs.birth_date.toordinal()) / _DAYS_PER_YEAR
    mean = true_age + cal.bias_at(np.clip(true_age, 0, None), obs.person_id)
    sd = cal.sigma_at(np.clip(true_age, 0, None))
    gauss = np.exp(-0.5 * ((obs.estimated_age - mean) / sd) ** 2) / (sd * np.sqrt(2 * np.pi))
    # Before the person's birth the face cannot be theirs: only the outlier explanation remains.
    gauss = np.where(true_age < 0, 0.0, gauss)
    inlier = (1 - OUTLIER_WEIGHT) * gauss
    return np.log(inlier + OUTLIER_WEIGHT * _OUTLIER_DENSITY), inlier


def estimate_date(
    observations: list[AgeObservation],
    calibration: Calibration,
    *,
    not_before: date | None = None,
    not_after: date | None = None,
    step_days: int = 7,
    asset_count: int = 1,
) -> DateEstimate:
    if not observations:
        raise EstimationError("No faces of people with a known birthday.")

    upper = min(not_after or date.today(), date.today())
    # All people in the photo must be born, but a single mis-tagged face should not force that,
    # so the grid starts at the oldest birthday and the likelihood handles the rest.
    lower = min(o.birth_date for o in observations)
    lower = max(lower, not_before or lower, date(MIN_YEAR, 1, 1))
    if lower > upper:
        raise EstimationError(
            f"No possible date: the earliest date ({lower}) is after the latest ({upper}). "
            "Check the birthdays and the date limits."
        )

    ordinals = np.arange(lower.toordinal(), upper.toordinal() + 1, step_days, dtype=float)
    total = np.zeros_like(ordinals)
    inliers = []
    # Several faces of one person (same event, several photos) have strongly correlated errors, so
    # together they count as one observation: the geometric mean of their likelihoods.
    per_person = Counter(o.person_id for o in observations)
    for obs in observations:
        ll, inlier = _log_likelihood(obs, ordinals, calibration)
        total += ll / per_person[obs.person_id]
        inliers.append(inlier)

    post = np.exp(total - total.max())
    post /= post.sum()
    cdf = np.cumsum(post)

    def quantile(q: float) -> date:
        return date.fromordinal(int(ordinals[min(np.searchsorted(cdf, q), len(ordinals) - 1)]))

    median = quantile(0.5)
    i_med = int(np.searchsorted(ordinals, median.toordinal()))
    for obs, inlier in zip(observations, inliers):
        obs.age_at_estimate = round((median.toordinal() - obs.birth_date.toordinal()) / _DAYS_PER_YEAR, 1)
        p_in = float((post * inlier).sum())
        p_out = OUTLIER_WEIGHT * _OUTLIER_DENSITY
        obs.outlier = bool(p_in < p_out or inlier[i_med] < p_out)

    return DateEstimate(
        median=median,
        low=quantile(0.05),
        high=quantile(0.95),
        mode=date.fromordinal(int(ordinals[int(post.argmax())])),
        observations=observations,
        asset_count=asset_count,
    )
