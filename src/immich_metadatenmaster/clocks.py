"""Working out a camera's clock offset automatically.

Most system cameras keep their own clock, which is often off (still on winter time, on the home time
zone, or simply wrong). Phones set their clocks themselves and store the time zone. The offset is found
in two ways:

1. Shared moments: you often take a picture of the same moment with both. Shifting the camera's photos
   by the right offset lines many of them up with phone photos (within 2 minutes), by any other offset
   only a few.
2. Similar photos: Immich's smart search finds, for a few camera photos, the most similar phone photos.
   Each pair votes for an offset; the true one gets many votes.

Nothing is written: the result is a suggestion for the clock offset used when searching locations.
"""

from __future__ import annotations

import bisect
import statistics
from dataclasses import dataclass
from typing import Any, Callable

# How far off a camera clock may be.
SPAN_S = 26 * 3600
# Two photos within this of each other (after the shift) show the same moment.
MATCH_S = 120
# Photos further apart than this are different moments (a burst of shots is one moment).
MOMENT_GAP_S = 15 * 60
# Camera photos compared with similar phone photos.
VISUAL_SAMPLES = 12
# Similar-photo pairs whose offsets lie within this of each other agree.
AGREE_S = 180


# Phones set their clocks from the network; cameras keep their own, even when they store a time zone.
PHONE_MAKES = {
    "apple", "google", "samsung", "xiaomi", "redmi", "poco", "realme", "oneplus", "oppo", "vivo", "huawei", "honor",
    "motorola", "nokia", "hmd global", "nothing", "fairphone", "lge", "lg electronics", "zte", "tcl", "meizu",
    "infinix", "tecno", "asus", "blackview", "ulefone", "cat", "shift",
}


def is_phone(make: str | None, model: str | None) -> bool:
    make, model = (make or "").strip().lower(), (model or "").strip().lower()
    if make in PHONE_MAKES or make.startswith(("xiaomi", "huawei", "samsung")):
        return True
    if make.startswith("sony") and model.startswith(("xq-", "so-", "sog", "sov", "xperia")):
        return True
    return model.startswith(("iphone", "pixel", "sm-", "galaxy"))


@dataclass(frozen=True)
class Shot:
    id: str
    file_name: str
    camera: str
    ts: float  # unix seconds: the true instant for reference photos, the camera clock's for camera photos
    local: str | None = None

    def brief(self) -> dict[str, Any]:
        return {"id": self.id, "file_name": self.file_name, "camera": self.camera, "local": self.local}


def _moments(times: list[float]) -> int:
    """How many separate moments these photos show (shots within 15 minutes are one)."""
    count, last = 0, None
    for t in sorted(times):
        if last is None or t - last > MOMENT_GAP_S:
            count += 1
        last = t
    return count


class _References:
    """Photos whose clock is right, sorted by time."""

    def __init__(self, refs: list[Shot]) -> None:
        self.shots = sorted(refs, key=lambda r: r.ts)
        self.times = [r.ts for r in self.shots]

    def around(self, t: float, span: float) -> list[Shot]:
        lo, hi = bisect.bisect_left(self.times, t - span), bisect.bisect_right(self.times, t + span)
        return self.shots[lo:hi]


def _line_up(cam: list[Shot], refs: _References, offset: float) -> list[tuple[Shot, Shot, float]]:
    """Camera photos that show the same moment as a reference photo once shifted by `offset`: (photo,
    reference, its exact offset)."""
    out = []
    for c in cam:
        best = None
        for r in refs.around(c.ts + offset, MATCH_S):
            d = r.ts - c.ts
            if best is None or abs(d - offset) < abs(best[2] - offset):
                best = (c, r, d)
        if best:
            out.append(best)
    return out


def _timing(cam: list[Shot], refs: _References) -> tuple[float | None, int, int, list[tuple[Shot, Shot, float]]]:
    """The offset that lines up the most moments: (offset, moments, moments of the runner-up more than
    10 minutes away, pairs)."""
    votes: dict[int, int] = {}
    for c in cam:
        bins: set[int] = set()
        for r in refs.around(c.ts, SPAN_S):
            m = round((r.ts - c.ts) / 60)
            bins.update(range(m - 2, m + 3))
        for b in bins:
            votes[b] = votes.get(b, 0) + 1
    if not votes:
        return None, 0, 0, []
    scored = []
    for b in sorted(votes, key=lambda k: -votes[k])[:40]:
        pairs = _line_up(cam, refs, b * 60)
        scored.append((_moments([p[0].ts for p in pairs]), len(pairs), b * 60, pairs))
    scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
    moments, _, start, pairs = scored[0]
    offset = statistics.median(p[2] for p in pairs) if pairs else start
    pairs = _line_up(cam, refs, offset)
    runner_up = max((s[0] for s in scored if abs(s[2] - offset) > 600), default=0)
    return offset, moments, runner_up, pairs


def _sample(cam: list[Shot], n: int) -> list[Shot]:
    """Photos spread over the whole time, to compare by looks."""
    ordered = sorted(cam, key=lambda c: c.ts)
    if len(ordered) <= n:
        return ordered
    step = (len(ordered) - 1) / (n - 1)
    return [ordered[round(i * step)] for i in range(n)]


def _agreeing(votes: list[tuple[Shot, Shot, float]]) -> tuple[float | None, list[tuple[Shot, Shot, float]]]:
    """The largest group of similar-photo pairs that agree on an offset, and its median."""
    best: list[tuple[Shot, Shot, float]] = []
    for _, _, d in votes:
        group = [v for v in votes if abs(v[2] - d) <= AGREE_S]
        if len(group) > len(best):
            best = group
    return (statistics.median(v[2] for v in best) if best else None), best


def estimate(cam: list[Shot], refs: list[Shot], similar: Callable[[Shot], list[str]] | None = None
             ) -> dict[str, Any]:
    """The clock offset of one camera (seconds to add to its clock), with how sure it is.

    `similar(photo)` returns the ids of the photos that look most like it, best first (None: no smart
    search); only reference photos among them count.
    """
    references = _References(refs)
    by_id = {r.id: r for r in refs}
    offset, moments, runner_up, pairs = _timing(cam, references)

    votes: list[tuple[Shot, Shot, float]] = []
    checked = 0
    if similar is not None:
        for c in _sample(cam, VISUAL_SAMPLES):
            ids = similar(c)
            checked += 1
            ref = next((by_id[i] for i in ids if i in by_id and abs(by_id[i].ts - c.ts) <= SPAN_S), None)
            if ref is not None:
                votes.append((c, ref, ref.ts - c.ts))
    visual_offset, agreeing = _agreeing(votes)

    # Clearly ahead of any other offset (a phone that shot all day lines up with almost any offset).
    timing_sure = offset is not None and moments >= max(3, runner_up + 2, 1.5 * runner_up)
    visual_sure = len(agreeing) >= 3 and len(agreeing) * 2 >= len(votes)
    visual_agrees = visual_offset is not None and offset is not None and abs(visual_offset - offset) <= AGREE_S
    if visual_sure and visual_agrees and moments >= 2:
        confidence = "high"
    elif timing_sure and not visual_sure:
        confidence = "high" if visual_agrees and len(agreeing) >= 2 else "medium"
    elif visual_sure:
        # The photos' looks are the stronger sign (e.g. both cameras shot all day, so times line up anywhere).
        offset, confidence = visual_offset, "medium"
        pairs = _line_up(cam, references, offset)  # type: ignore[arg-type]
        moments = _moments([p[0].ts for p in pairs])
    elif offset is not None and moments >= 2:
        confidence = "low"
    else:
        offset, confidence = None, "none"

    examples = []
    if offset is not None:
        seen = set()
        for c, r, d in [v for v in votes if abs(v[2] - offset) <= AGREE_S] + pairs:
            if c.id not in seen and len(examples) < 4:
                seen.add(c.id)
                examples.append({"photo": c.brief(), "reference": r.brief(), "offset_s": round(d)})
    return {
        "offset_s": round(offset) if offset is not None else None, "confidence": confidence,
        "moments": moments if offset is not None else 0, "matched": len(pairs) if offset is not None else 0,
        "pairs_checked": checked, "pairs_found": len(votes),
        "pairs_agreeing": sum(1 for v in votes if offset is not None and abs(v[2] - offset) <= AGREE_S),
        "examples": examples,
    }
