"""Recorded tours straight from Komoot, instead of exporting GPX files by hand.

Komoot has no public API; this uses the one its own apps use (as the open-source tools komootgpx and
kompy do). It may stop working whenever Komoot changes it, and Komoot's terms may not allow it. The
email and password are only sent once, to get a token; only the token is kept.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote
from xml.sax.saxutils import escape

import httpx

API = "https://api.komoot.de"
PAGE_SIZE = 50
# The file name a tour gets as a track, to tell which tours are imported already.
FILE_PREFIX = "komoot-"


class KomootError(RuntimeError):
    pass


class KomootLoginFailed(KomootError):
    """Komoot does not accept the email and password (or the token any more)."""


def file_name(tour_id: str) -> str:
    return f"{FILE_PREFIX}{tour_id}.gpx"


def tour_of(file: str) -> str | None:
    """The tour id of a track imported from Komoot."""
    if file.startswith(FILE_PREFIX) and file.endswith(".gpx"):
        return file[len(FILE_PREFIX):-4]
    return None


class Komoot:
    def __init__(self, user_id: str = "", token: str = "", *, transport: httpx.BaseTransport | None = None,
                 timeout: float = 30.0) -> None:
        self.user_id, self.token = user_id, token
        self._http = httpx.Client(base_url=API, timeout=timeout, transport=transport,
                                  headers={"Accept": "application/hal+json, application/json"})

    def close(self) -> None:
        self._http.close()

    def _get(self, path: str, auth: tuple[str, str] | None = None, **params: Any) -> Any:
        try:
            resp = self._http.get(path, params=params or None, auth=auth or (self.user_id, self.token))
        except httpx.HTTPError as exc:
            raise KomootError(f"Komoot cannot be reached: {exc}") from exc
        if resp.status_code in (401, 403):
            raise KomootLoginFailed("Komoot did not accept the login.")
        if resp.status_code >= 400:
            raise KomootError(f"Komoot answered {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError:
            raise KomootError("Komoot sent something unexpected (has its API changed?).") from None

    def log_in(self, email: str, password: str) -> dict[str, str]:
        """Exchange email and password for a token: {"user_id", "token", "name"}."""
        try:
            data = self._get(f"/v006/account/email/{quote(email, safe='@')}/", auth=(email, password))
            self.user_id, self.token = str(data["username"]), str(data["password"])
        except KomootLoginFailed:
            raise KomootLoginFailed("Wrong email or password for Komoot.") from None
        except (KeyError, TypeError):
            raise KomootError("Komoot sent something unexpected (has its API changed?).") from None
        user = data.get("user") or {}
        return {"user_id": self.user_id, "token": self.token, "name": user.get("displayname") or email}

    def tours(self, page: int = 0) -> dict[str, Any]:
        """One page of recorded tours, newest first."""
        data = self._get(f"/v007/users/{quote(self.user_id)}/tours/", type="tour_recorded", sort_field="date",
                         sort_direction="desc", page=page, limit=PAGE_SIZE)
        try:
            items = (data.get("_embedded") or {}).get("tours") or []
            tours = [{"id": str(t["id"]), "name": t.get("name") or f"Tour {t['id']}", "date": t.get("date"),
                      "distance_m": t.get("distance"), "duration_s": t.get("duration"), "sport": t.get("sport")}
                     for t in items]
            info = data.get("page") or {}
        except (AttributeError, KeyError, TypeError):
            raise KomootError("Komoot sent something unexpected (has its API changed?).") from None
        return {"tours": tours, "page": int(info.get("number", page)), "pages": int(info.get("totalPages", 1))}

    def gpx(self, tour_id: str) -> tuple[str, bytes]:
        """A recorded tour as GPX with the time of every point: (its name, the file)."""
        tour = self._get(f"/v007/tours/{quote(tour_id)}", _embedded="coordinates", format="coordinate_array")
        try:
            name = tour.get("name") or f"Tour {tour_id}"
            items = tour["_embedded"]["coordinates"]["items"]
            start = _parse_date(tour.get("date"))
        except (KeyError, TypeError):
            raise KomootError("Komoot sent no track points for this tour.") from None
        if not items:
            raise KomootError(f"The tour “{name}” has no track points.")
        return name, to_gpx(name, items, start)


def _parse_date(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def to_gpx(name: str, items: list[dict[str, Any]], start: datetime | None) -> bytes:
    """Komoot's points ({lat, lng, alt, t}) as GPX. `t` is in milliseconds: since the start of the tour
    (the first point is 0), or since 1970."""
    first_t = items[0].get("t")
    relative = first_t is not None and first_t < 10**11  # before 1973: not a date
    out = []
    for p in items:
        if "lat" not in p or "lng" not in p:
            continue
        parts = [f'<trkpt lat="{float(p["lat"]):.7f}" lon="{float(p["lng"]):.7f}">']
        if p.get("alt") is not None:
            parts.append(f"<ele>{float(p['alt']):.1f}</ele>")
        t = p.get("t")
        if t is not None and (start is not None or not relative):
            when = start + timedelta(milliseconds=t) if relative else \
                datetime.fromtimestamp(t / 1000, timezone.utc)  # type: ignore[operator]
            parts.append(f"<time>{when.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3]}Z</time>")
        out.append("".join(parts) + "</trkpt>")
    title = escape(name)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<gpx version="1.1" creator="Immich Metadatenmaster (Komoot)" '
            'xmlns="http://www.topografix.com/GPX/1/1">'
            f"<metadata><name>{title}</name></metadata><trk><name>{title}</name><trkseg>"
            + "".join(out) + "</trkseg></trk></gpx>").encode()
