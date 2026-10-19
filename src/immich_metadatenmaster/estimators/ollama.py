"""Age estimation with a local vision language model served by Ollama (https://ollama.com).

Slower and less precise than MiVOLO, but needs no Python ML stack and copes with faded scans
reasonably well. Try e.g. `ollama pull qwen2.5vl:7b` or `gemma3:12b`.
"""

from __future__ import annotations

import base64
import io
import json
import re

import httpx
from PIL import Image

_PROMPT = (
    "This is a cropped face from a family photo. Estimate the person's age in years as precisely "
    "as you can, judging only by their appearance. Answer with JSON only: {\"age\": <number>}"
)


class OllamaEstimator:
    face_margin = 0.5

    def __init__(self, url: str, model: str, *, transport: httpx.BaseTransport | None = None) -> None:
        self._model = model
        self._http = httpx.Client(base_url=url, timeout=300, transport=transport)

    def estimate(self, faces: list[Image.Image]) -> list[float]:
        return [self._estimate_one(f) for f in faces]

    def _estimate_one(self, face: Image.Image) -> float:
        buf = io.BytesIO()
        face.convert("RGB").save(buf, format="JPEG", quality=92)
        resp = self._http.post("/api/generate", json={
            "model": self._model,
            "prompt": _PROMPT,
            "images": [base64.b64encode(buf.getvalue()).decode()],
            "format": "json",
            "stream": False,
            "options": {"temperature": 0},
        })
        resp.raise_for_status()
        text = resp.json().get("response", "")
        return parse_age(text)


def parse_age(text: str) -> float:
    try:
        value = json.loads(text)["age"]
        if isinstance(value, str):
            raise TypeError
        return float(value)
    except (ValueError, KeyError, TypeError):
        m = re.search(r"\d+(?:\.\d+)?", text)
        if not m:
            raise ValueError(f"Model gave no age: {text[:100]!r}") from None
        return float(m.group())
