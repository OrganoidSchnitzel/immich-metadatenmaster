"""Dates in file names, e.g. from phones and messengers that do not keep the EXIF date.

IMG_20190713_142233.jpg · PXL_20230101_123456789.jpg · IMG-20190713-WA0001.jpg ·
WhatsApp Image 2020-01-01 at 12.00.00.jpeg · Screenshot_20200101-120000.png · 1987-07-01 Holiday.jpg
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time

MIN_YEAR = 1900  # also scans named by date, e.g. "1987-07-01 Holiday.jpg"

_DATE = r"(?P<y>(?:19|20)\d{2})(?P<s1>[-_.]?)(?P<m>0[1-9]|1[0-2])(?P=s1)(?P<d>0[1-9]|[12]\d|3[01])"
_TIME = r"(?:[ _T-]|\s+at\s+)(?P<H>[01]\d|2[0-3])(?P<s2>[-_.:]?)(?P<M>[0-5]\d)(?P=s2)(?P<S>[0-5]\d)"
# Optional milliseconds after the time (PXL_20230101_123456789); no digits may follow the match.
_PATTERN = re.compile(rf"(?<!\d){_DATE}(?:{_TIME}(?:[._]?\d{{1,3}})?)?(?!\d)", re.I)


@dataclass(frozen=True)
class NameDate:
    day: date
    time: time | None  # None: the name only contains the day

    @property
    def label(self) -> str:
        return f"{self.day.isoformat()} {self.time.isoformat()}" if self.time else self.day.isoformat()

    def to_datetime(self) -> datetime:
        return datetime.combine(self.day, self.time or time(12, 0))


def date_from_name(file_name: str, today: date | None = None) -> NameDate | None:
    """The date (and time, if present) in a file name, or None."""
    today = today or date.today()
    stem = file_name.rsplit(".", 1)[0]
    for m in _PATTERN.finditer(stem):
        try:
            day = date(int(m["y"]), int(m["m"]), int(m["d"]))
        except ValueError:
            continue
        if not MIN_YEAR <= day.year or day > today:
            continue
        t = time(int(m["H"]), int(m["M"]), int(m["S"])) if m["H"] is not None else None
        return NameDate(day, t)
    return None


def differs(name_date: NameDate, current: datetime | None, *, utc: datetime | None = None,
            minutes: int = 2) -> bool:
    """Whether Immich's (local) date disagrees with the file name enough to suggest a change.

    Some phones (Google Pixel) name their files by UTC time, so a name matching the photo's UTC time
    (`utc`, naive) agrees as well.
    """
    if current is None:
        return True

    def off(when: datetime) -> bool:
        if name_date.time is None:
            return when.date() != name_date.day
        return abs((when - name_date.to_datetime()).total_seconds()) > minutes * 60

    return off(current) and (utc is None or off(utc))
