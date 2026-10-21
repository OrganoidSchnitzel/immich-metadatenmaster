"""Address search for the map, via a Photon geocoder (https://photon.komoot.io or a self-hosted one).

The server asks Photon, not the browser, so the geocoder can be changed in one place. Coordinates
typed or pasted directly ("47.4211, 10.9853", e.g. from Google Maps) are understood without asking.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

# Languages the public Photon instance knows; for others it returns local names.
PHOTON_LANGUAGES = {"en", "de", "fr"}
_COORDS = re.compile(r"^\s*(-?\d{1,2}(?:[.,]\d+)?)\s*[,;\s]\s*(-?\d{1,3}(?:[.,]\d+)?)\s*$")
_USER_AGENT = "immich-metadatenmaster (https://github.com/OrganoidSchnitzel/immich-metadatenmaster)"


class GeocodeError(RuntimeError):
    pass


def parse_coordinates(text: str) -> tuple[float, float] | None:
    """"47.4211, 10.9853" or "47.4211 10.9853" → (lat, lon)."""
    m = _COORDS.match(text)
    if not m:
        return None
    lat, lon = (float(x.replace(",", ".")) for x in (m[1], m[2]))
    if -90 <= lat <= 90 and -180 <= lon <= 180:
        return lat, lon
    return None


def _label(p: dict[str, Any]) -> tuple[str, str]:
    """A main line (the place) and a second line (where it is)."""
    street = " ".join(x for x in (p.get("street"), p.get("housenumber")) if x)
    name = p.get("name") or street or p.get("city") or p.get("country") or "?"
    town = " ".join(x for x in (p.get("postcode"), p.get("city") or p.get("locality")) if x)
    parts = [street if street != name else "", p.get("district") if p.get("district") != name else "",
             town if p.get("city") != name else "", p.get("state") if p.get("state") != name else "",
             p.get("country") if p.get("country") != name else ""]
    seen: list[str] = []
    for part in parts:
        if part and part not in seen:
            seen.append(part)
    return name, ", ".join(seen)


def search(base_url: str, query: str, *, lang: str | None = None, near: tuple[float, float] | None = None,
           limit: int = 8, transport: httpx.BaseTransport | None = None) -> list[dict[str, Any]]:
    """Places matching `query`, best first: {name, detail, lat, lon, bounds, kind}."""
    query = query.strip()
    if not query:
        return []
    if (coords := parse_coordinates(query)) is not None:
        lat, lon = coords
        return [{"name": f"{lat:.6f}, {lon:.6f}", "detail": "Coordinates", "lat": lat, "lon": lon,
                 "bounds": None, "kind": "coordinates"}]
    if not base_url:
        raise GeocodeError("Address search is switched off in the settings.")
    params: dict[str, Any] = {"q": query[:200], "limit": limit}
    if lang and lang.lower()[:2] in PHOTON_LANGUAGES:
        params["lang"] = lang.lower()[:2]
    if near is not None:
        params["lat"], params["lon"] = round(near[0], 4), round(near[1], 4)
    try:
        with httpx.Client(timeout=10, transport=transport, headers={"User-Agent": _USER_AGENT}) as client:
            resp = client.get(f"{base_url.rstrip('/')}/api", params=params)
    except httpx.HTTPError as exc:
        raise GeocodeError(f"The address search is not reachable: {exc}") from exc
    if resp.status_code >= 400:
        raise GeocodeError(f"The address search answered {resp.status_code}.")
    try:
        features = resp.json().get("features") or []
    except ValueError as exc:
        raise GeocodeError("The address search sent an invalid answer.") from exc

    out = []
    for f in features:
        try:
            lon, lat = (float(v) for v in f["geometry"]["coordinates"][:2])
        except (KeyError, TypeError, ValueError):
            continue
        p = f.get("properties") or {}
        name, detail = _label(p)
        bounds = None
        if isinstance(p.get("extent"), list) and len(p["extent"]) == 4:
            west, north, east, south = (float(v) for v in p["extent"])
            bounds = [[south, west], [north, east]]
        out.append({"name": name, "detail": detail, "lat": lat, "lon": lon, "bounds": bounds,
                    "kind": p.get("osm_value") or p.get("type") or ""})
    return out
