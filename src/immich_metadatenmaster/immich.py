"""Minimal client for the parts of the Immich REST API this tool needs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Iterator

import httpx


class ImmichError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        # The HTTP status Immich answered with (None: no answer).
        self.status = status


@dataclass(frozen=True)
class Person:
    id: str
    name: str
    birth_date: date | None
    is_hidden: bool = False

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> "Person":
        return cls(
            id=d["id"],
            name=d.get("name") or "",
            birth_date=_parse_date(d.get("birthDate")),
            is_hidden=bool(d.get("isHidden")),
        )


@dataclass(frozen=True)
class Face:
    id: str
    person: Person | None
    x1: int
    y1: int
    x2: int
    y2: int
    # Size of the image the bounding box refers to (Immich detects on the preview).
    image_width: int
    image_height: int

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> "Face":
        person = d.get("person")
        return cls(
            id=d["id"],
            person=Person.from_api(person) if person else None,
            x1=d["boundingBoxX1"],
            y1=d["boundingBoxY1"],
            x2=d["boundingBoxX2"],
            y2=d["boundingBoxY2"],
            image_width=d["imageWidth"],
            image_height=d["imageHeight"],
        )


@dataclass
class Asset:
    id: str
    file_name: str
    type: str
    # Local capture time as shown in the Immich timeline (naive).
    local_datetime: datetime | None
    # EXIF DateTimeOriginal and camera make, used to decide if a date is trustworthy.
    exif_datetime: datetime | None = None
    camera_make: str | None = None
    camera_model: str | None = None
    description: str = ""
    people: list[Person] = field(default_factory=list)
    # The capture time as an instant (UTC), as Immich understands it, and the photo's own time zone
    # (None when the file does not say; Immich then treats the camera's clock time as UTC).
    taken_at: datetime | None = None
    time_zone: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    # "timeline", "archive", "hidden" (e.g. the video half of a Live Photo) or "locked".
    visibility: str = "timeline"
    original_path: str = ""

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> "Asset":
        exif = d.get("exifInfo") or {}
        return cls(
            id=d["id"],
            file_name=d.get("originalFileName") or "",
            type=d.get("type") or "IMAGE",
            local_datetime=_parse_naive_datetime(d.get("localDateTime")),
            exif_datetime=_parse_naive_datetime(exif.get("dateTimeOriginal")),
            camera_make=exif.get("make") or None,
            camera_model=exif.get("model") or None,
            description=exif.get("description") or "",
            people=[Person.from_api(p) for p in d.get("people") or []],
            taken_at=_parse_utc_datetime(d.get("fileCreatedAt")),
            time_zone=exif.get("timeZone") or None,
            latitude=_float(exif.get("latitude")),
            longitude=_float(exif.get("longitude")),
            visibility=d.get("visibility") or "timeline",
            original_path=d.get("originalPath") or "",
        )

    @property
    def shown(self) -> bool:
        """Visible in Immich's timeline or archive (not a hidden Live Photo video, not locked)."""
        return self.visibility in {"timeline", "archive"}

    @property
    def has_location(self) -> bool:
        return self.latitude is not None and self.longitude is not None

    @property
    def camera(self) -> str:
        """Camera model as shown to the user, e.g. "Canon EOS 80D"."""
        make, model = self.camera_make or "", self.camera_model or ""
        if make and model.lower().startswith(make.split()[0].lower()):
            return model
        return f"{make} {model}".strip()


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value[:10])


def _float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _parse_utc_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def iso_utc(dt: datetime) -> str:
    """The format Immich's search filters expect, e.g. 2024-01-01T00:00:00.000Z."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _parse_naive_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    # Immich returns e.g. "1987-07-01T12:00:00.000Z"; localDateTime uses "Z" for a local time.
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


class ImmichClient:
    def __init__(self, base_url: str, api_key: str = "", *, access_token: str = "",
                 transport: httpx.BaseTransport | None = None, timeout: float = 60.0) -> None:
        base = base_url.rstrip("/")
        if not base.endswith("/api"):
            base += "/api"
        self._http = httpx.Client(
            base_url=base,
            headers={"Accept": "application/json", "User-Agent": "Immich Metadatenmaster"},
            timeout=timeout,
            transport=transport,
        )
        self._send_timezone = True
        self.set_credentials(api_key=api_key, access_token=access_token)

    def set_credentials(self, *, api_key: str = "", access_token: str = "") -> None:
        """Act with an API key, or with the session token of a login."""
        self._http.headers.pop("x-api-key", None)
        self._http.headers.pop("Authorization", None)
        if access_token:
            self._http.headers["Authorization"] = f"Bearer {access_token}"
        elif api_key:
            self._http.headers["x-api-key"] = api_key

    @property
    def credentials(self) -> tuple[str, str]:
        """("api_key" or "token", the secret), or ("", "") without credentials."""
        if auth := self._http.headers.get("Authorization"):
            return "token", auth.removeprefix("Bearer ")
        if key := self._http.headers.get("x-api-key"):
            return "api_key", key
        return "", ""

    def close(self) -> None:
        self._http.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            resp = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ImmichError(f"{method} {path} failed: {exc}") from exc
        if resp.status_code >= 400:
            detail = resp.text[:300]
            raise ImmichError(f"{method} {path} returned {resp.status_code}: {detail}", resp.status_code)
        return resp

    def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        resp = self._request(method, path, **kwargs)
        return resp.json() if resp.content else None

    # --- server ---------------------------------------------------------------------------

    def server_version(self) -> str:
        v = self._json("GET", "/server/version")
        return f"{v['major']}.{v['minor']}.{v['patch']}"

    # --- users and logins -------------------------------------------------------------------

    def login(self, email: str, password: str) -> dict[str, Any]:
        """Log in with an Immich account: the user (id, email, name, admin) and a session token."""
        d = self._json("POST", "/auth/login", json={"email": email, "password": password})
        return {"id": d["userId"], "email": d.get("userEmail") or email, "name": d.get("name") or "",
                "is_admin": bool(d.get("isAdmin")), "token": d["accessToken"]}

    def me(self) -> dict[str, Any]:
        """The user these credentials belong to (needs user.read for an API key)."""
        d = self._json("GET", "/users/me")
        return {"id": d["id"], "email": d.get("email") or "", "name": d.get("name") or "",
                "is_admin": bool(d.get("isAdmin"))}

    def logout(self) -> None:
        """End the session of a login (its token stops working)."""
        self._request("POST", "/auth/logout")

    # --- albums and assets ------------------------------------------------------------------

    def albums(self) -> list[dict[str, Any]]:
        return self._json("GET", "/albums")

    def album(self, album_id: str) -> dict[str, Any]:
        return self._json("GET", f"/albums/{album_id}")

    def find_album(self, name_or_id: str) -> dict[str, Any]:
        for album in self.albums():
            if album["id"] == name_or_id or album["albumName"] == name_or_id:
                return album
        raise ImmichError(f"No album named or with id '{name_or_id}'")

    def search_pages(self, size: int = 1000, **filters: Any) -> Iterator[list[Asset]]:
        """A metadata search, page by page (with EXIF, without people unless asked)."""
        page: int | None = 1
        while page is not None:
            body = {"withExif": True, "withPeople": False, "size": size, "page": page, **filters}
            result = self._json("POST", "/search/metadata", json=body)["assets"]
            yield [Asset.from_api(item) for item in result["items"]]
            next_page = result.get("nextPage")
            page = int(next_page) if next_page else None

    def search_assets(self, **filters: Any) -> Iterator[Asset]:
        for page in self.search_pages(size=250, **{"withPeople": True, **filters}):
            yield from page

    def count_assets(self, **filters: Any) -> int | None:
        """How many assets a search finds (None if the API key may not ask: asset.statistics)."""
        try:
            return int(self._json("POST", "/search/statistics", json=filters)["total"])
        except (ImmichError, KeyError, TypeError, ValueError):
            return None

    def similar_assets(self, asset_id: str, *, taken_after: datetime, taken_before: datetime,
                       size: int = 8) -> list[Asset]:
        """The photos that look most like this one, best first (Immich's smart search, which needs its
        machine learning)."""
        body = {"queryAssetId": asset_id, "takenAfter": iso_utc(taken_after), "takenBefore": iso_utc(taken_before),
                "size": size, "withExif": True}
        return [Asset.from_api(item) for item in self._json("POST", "/search/smart", json=body)["assets"]["items"]]

    def album_all_assets(self, album_id: str) -> list[Asset]:
        """Photos and videos of an album."""
        return list(self.search_assets(albumIds=[album_id], withPeople=False))

    def album_assets(self, album_id: str) -> list[Asset]:
        """Photos and videos of an album, sorted by file name (hidden Live Photo videos left out)."""
        assets = (a for a in self.search_assets(albumIds=[album_id]) if a.shown)
        return sorted(assets, key=lambda a: a.file_name.lower())

    def person_assets(self, person_id: str) -> Iterator[Asset]:
        return self.search_assets(personIds=[person_id], type="IMAGE")

    def assets_taken_between(self, start: datetime, end: datetime, album_id: str | None = None) -> list[Asset]:
        """Photos and videos whose capture instant (as Immich stores it) lies in [start, end]."""
        filters: dict[str, Any] = {"takenAfter": iso_utc(start), "takenBefore": iso_utc(end), "withPeople": False}
        if album_id:
            filters["albumIds"] = [album_id]
        return list(self.search_assets(**filters))

    def asset(self, asset_id: str) -> Asset:
        return Asset.from_api(self._json("GET", f"/assets/{asset_id}"))

    def thumbnail(self, asset_id: str, size: str = "thumbnail") -> tuple[bytes, str]:
        resp = self._request("GET", f"/assets/{asset_id}/thumbnail", params={"size": size},
                             headers={"Accept": "*/*"})
        return resp.content, resp.headers.get("content-type", "image/jpeg")

    def original(self, asset_id: str) -> bytes:
        return self._request("GET", f"/assets/{asset_id}/original", headers={"Accept": "*/*"}).content

    def set_date(self, asset_ids: list[str], date_time_original: str, timezone: str | None) -> None:
        body: dict[str, Any] = {"ids": asset_ids, "dateTimeOriginal": date_time_original}
        if timezone and self._send_timezone:
            try:
                self._request("PUT", "/assets", json={**body, "timeZone": timezone})
                return
            except ImmichError as exc:
                # Older Immich versions do not know the timeZone field.
                if " 400:" not in str(exc):
                    raise
                self._send_timezone = False
        self._request("PUT", "/assets", json=body)

    def set_location(self, asset_ids: list[str], latitude: float, longitude: float) -> None:
        self._request("PUT", "/assets", json={"ids": asset_ids, "latitude": round(latitude, 7),
                                              "longitude": round(longitude, 7)})

    def remove_from_album(self, album_id: str, asset_ids: list[str]) -> int:
        """Remove assets from an album (they stay in the library). Returns how many were removed."""
        result = self._json("DELETE", f"/albums/{album_id}/assets", json={"ids": asset_ids})
        return sum(1 for r in result or [] if r.get("success") or r.get("error") == "not_found")

    def set_description(self, asset_id: str, description: str) -> None:
        self._request("PUT", f"/assets/{asset_id}", json={"description": description})

    # --- people and faces -------------------------------------------------------------------

    def people(self) -> list[Person]:
        people: list[Person] = []
        page = 1
        while True:
            data = self._json("GET", "/people", params={"page": page, "size": 1000, "withHidden": "true"})
            people.extend(Person.from_api(p) for p in data["people"])
            if not data.get("hasNextPage"):
                return people
            page += 1

    def set_birth_date(self, person_id: str, birth_date: date | None) -> Person:
        body = {"birthDate": birth_date.isoformat() if birth_date else None}
        return Person.from_api(self._json("PUT", f"/people/{person_id}", json=body))

    def person_thumbnail(self, person_id: str) -> tuple[bytes, str]:
        resp = self._request("GET", f"/people/{person_id}/thumbnail", headers={"Accept": "*/*"})
        return resp.content, resp.headers.get("content-type", "image/jpeg")

    def faces(self, asset_id: str) -> list[Face]:
        return [Face.from_api(f) for f in self._json("GET", "/faces", params={"id": asset_id})]

    # --- tags -------------------------------------------------------------------------------

    def _upsert_tag(self, tag_value: str) -> str:
        tags = self._json("PUT", "/tags", json={"tags": [tag_value]})
        return next((t for t in tags if t.get("value") == tag_value), tags[-1])["id"]

    def tag_assets(self, tag_value: str, asset_ids: list[str]) -> None:
        tag_id = self._upsert_tag(tag_value)
        self._request("PUT", "/tags/assets", json={"tagIds": [tag_id], "assetIds": asset_ids})

    def untag_assets(self, tag_value: str, asset_ids: list[str]) -> None:
        tag_id = self._upsert_tag(tag_value)
        self._request("DELETE", f"/tags/{tag_id}/assets", json={"ids": asset_ids})
