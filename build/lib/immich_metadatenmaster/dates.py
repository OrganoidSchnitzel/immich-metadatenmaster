"""Parsing of rough, user-supplied dates ("1987", "06.1987", "1980s") and turning them into timestamps."""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

MIN_YEAR = 1826  # the oldest surviving photograph


@dataclass(frozen=True)
class DateSpec:
    """A date known to some precision, as the inclusive range [start, end]."""

    start: date
    end: date
    precision: str  # "day" | "month" | "year" | "range"
    label: str

    def anchor(self, mode: str = "middle") -> date:
        if mode == "start" or self.precision == "day":
            return self.start
        return self.start + (self.end - self.start) / 2


_SEP = r"[.\-/ ]"


def parse_date_spec(text: str) -> DateSpec:
    """Parse a rough date. Accepted forms, e.g.:

    1987 · 1987-06 · 06/1987 · 6.1987 · 1987-06-14 · 14.06.1987 · 1985-1989 · 1980s · 80er · ca. 1987
    """
    s = text.strip().lower()
    s = re.sub(r"^(ca\.?|circa|about|approx\.?|um|~)\s*", "", s)
    s = s.replace("–", "-").replace("—", "-").strip()
    if not s:
        raise ValueError("Please enter a date.")

    if m := re.fullmatch(r"(\d{4})", s):
        y = _year(m[1])
        return DateSpec(date(y, 1, 1), date(y, 12, 31), "year", str(y))

    if m := re.fullmatch(r"(\d{2}|\d{4})(?:s|er|'s|er jahre)", s):
        y = int(m[1])
        if y < 100:
            y += 1900
        if y % 10:
            raise ValueError(f"'{text}' is not a decade.")
        _year(str(y))
        return DateSpec(date(y, 1, 1), date(y + 9, 12, 31), "range", f"{y}s")

    if m := re.fullmatch(r"(\d{4})\s*-\s*(\d{4})", s):
        a, b = _year(m[1]), _year(m[2])
        if b < a:
            raise ValueError("The range ends before it starts.")
        return DateSpec(date(a, 1, 1), date(b, 12, 31), "range", f"{a}–{b}")

    if m := re.fullmatch(rf"(\d{{4}}){_SEP}(\d{{1,2}})", s):
        return _month(int(m[1]), int(m[2]))
    if m := re.fullmatch(rf"(\d{{1,2}}){_SEP}(\d{{4}})", s):
        return _month(int(m[2]), int(m[1]))

    if m := re.fullmatch(rf"(\d{{4}}){_SEP}(\d{{1,2}}){_SEP}(\d{{1,2}})", s):
        return _day(int(m[1]), int(m[2]), int(m[3]))
    if m := re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", s):
        return _day(int(m[3]), int(m[2]), int(m[1]))

    raise ValueError(
        f"Could not understand '{text}'. Try e.g. 1987, 06.1987, 14.06.1987, 1985-1989 or 1980s."
    )


def _year(value: str) -> int:
    y = int(value)
    if not MIN_YEAR <= y <= date.today().year:
        raise ValueError(f"{y} is not a plausible year for a photo.")
    return y


def _month(y: int, m: int) -> DateSpec:
    _year(str(y))
    if not 1 <= m <= 12:
        raise ValueError(f"{m} is not a month.")
    last = calendar.monthrange(y, m)[1]
    return DateSpec(date(y, m, 1), date(y, m, last), "month", f"{y}-{m:02d}")


def _day(y: int, m: int, d: int) -> DateSpec:
    _year(str(y))
    try:
        day = date(y, m, d)
    except ValueError as exc:
        raise ValueError(f"{y}-{m:02d}-{d:02d} is not a valid date.") from exc
    return DateSpec(day, day, "day", day.isoformat())


def parse_year_bound(text: str | None, *, end: bool) -> date | None:
    """Parse an optional "not before"/"not after" bound: a year or anything parse_date_spec accepts."""
    if text is None or not str(text).strip():
        return None
    spec = parse_date_spec(str(text))
    return spec.end if end else spec.start


def timestamps(day: date, count: int, tz_name: str, *, spacing: timedelta = timedelta(minutes=1)) -> list[str]:
    """ISO timestamps at noon local time, `spacing` apart so the timeline keeps the given order."""
    tz = ZoneInfo(tz_name)
    base = datetime.combine(day, time(12, 0), tzinfo=tz)
    return [(base + i * spacing).isoformat(timespec="milliseconds") for i in range(count)]


def years_between(a: date, b: date) -> float:
    return (b.toordinal() - a.toordinal()) / 365.2425
