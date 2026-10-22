"""Address search and place names for the map, via a Photon geocoder (https://photon.komoot.io or a
self-hosted one).

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


def _get(base_url: str, path: str, params: dict[str, Any], transport: httpx.BaseTransport | None) -> Any:
    try:
        with httpx.Client(timeout=10, transport=transport, headers={"User-Agent": _USER_AGENT}) as client:
            resp = client.get(f"{base_url.rstrip('/')}{path}", params=params)
    except httpx.HTTPError as exc:
        raise GeocodeError(f"The address search is not reachable: {exc}") from exc
    if resp.status_code >= 400:
        raise GeocodeError(f"The address search answered {resp.status_code}.")
    try:
        return resp.json()
    except ValueError as exc:
        raise GeocodeError("The address search sent an invalid answer.") from exc


def _lang(lang: str | None) -> str | None:
    return lang.lower()[:2] if lang and lang.lower()[:2] in PHOTON_LANGUAGES else None


def place_label(p: dict[str, Any]) -> str:
    """A short name for where a point is: "Höllentalangerhütte, Grainau" or "Garmisch-Partenkirchen, Deutschland"."""
    parts: list[str] = []
    if p.get("name") and p.get("type") not in {"house", "street"}:
        parts.append(p["name"])
    town = p.get("city") or p.get("locality") or p.get("district") or p.get("county")
    if town and town not in parts:
        parts.append(town)
    if not parts and p.get("state"):
        parts.append(p["state"])
    if len(parts) < 2 and p.get("country") and p["country"] not in parts:
        parts.append(p["country"])
    return ", ".join(parts[:2])


def reverse(base_url: str, lat: float, lon: float, *, lang: str | None = None,
            transport: httpx.BaseTransport | None = None) -> str:
    """The name of the place at a point ("" if there is none)."""
    params: dict[str, Any] = {"lat": round(lat, 5), "lon": round(lon, 5), "limit": 1}
    if (code := _lang(lang)) is not None:
        params["lang"] = code
    features = (_get(base_url, "/reverse", params, transport) or {}).get("features") or []
    return place_label(features[0].get("properties") or {}) if features else ""


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
    if (code := _lang(lang)) is not None:
        params["lang"] = code
    if near is not None:
        params["lat"], params["lon"] = round(near[0], 4), round(near[1], 4)
    features = (_get(base_url, "/api", params, transport) or {}).get("features") or []

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
