from __future__ import annotations

from typing import Protocol

from PIL import Image


class AgeEstimator(Protocol):
    #: Extra context around Immich's face box, as a fraction of the face size per side.
    face_margin: float

    def estimate(self, faces: list[Image.Image]) -> list[float]:
        """Return the estimated age in years for each face crop."""
        ...
