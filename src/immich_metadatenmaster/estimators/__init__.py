"""Age estimation backends."""

from __future__ import annotations

from ..config import Settings
from .base import AgeEstimator


# Uncalibrated spread of each backend relative to the default error model.
_SIGMA_SCALE = {"mivolo": 1.0, "ollama": 1.5}


def backend_name(settings: Settings) -> str:
    """Stable identifier of the configured model; cached ages and calibrations are keyed by it."""
    if settings.estimator == "ollama":
        return f"ollama:{settings.ollama_model}"
    return f"{settings.estimator}:{settings.mivolo_model}"


def default_sigma_scale(settings: Settings) -> float:
    return _SIGMA_SCALE.get(settings.estimator, 1.5)


def create_estimator(settings: Settings) -> AgeEstimator:
    if settings.estimator == "mivolo":
        from .mivolo import MiVoloEstimator

        return MiVoloEstimator(settings.mivolo_model, device=settings.device)
    if settings.estimator == "ollama":
        from .ollama import OllamaEstimator

        return OllamaEstimator(settings.ollama_url, settings.ollama_model)
    raise SystemExit(f"Unknown AGE_ESTIMATOR '{settings.estimator}' (use 'mivolo' or 'ollama').")


__all__ = ["AgeEstimator", "backend_name", "create_estimator", "default_sigma_scale"]
