"""A fake Immich server (httpx MockTransport) and a fake age model.

Every face is drawn as a solid square whose grey level encodes the person's true age at the photo
date, and the fake model reads it back (plus a configurable per-person bias). This exercises the
whole pipeline: bounding box scaling, cropping, caching, inference and writing back.
"""

from __future__ import annotations

import io
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from zoneinfo import ZoneInfo

import httpx
import numpy as np
import pytest
from PIL import Image

from immich_metadatenmaster.config import Settings
from immich_metadatenmaster.dates import years_between
from immich_metadatenmaster.geotag import GeoTagger
from immich_metadatenmaster.immich import ImmichClient
from immich_metadatenmaster.service import AgeGuesser
from immich_metadatenmaster.store import Store

PREVIEW_W, PREVIEW_H = 600, 400
ORIGINAL_SCALE = 4


@dataclass
class FakeFace:
    id: str
    person_id: str | None
    box: tuple[int, int, int, int]  # in preview coordinates


@dataclass
class FakeAsset:
    id: str
    file_name: str
    taken: date  # the true date, used to draw ages
    local_datetime: str | None = None
    exif_date: str | None = None
    make: str | None = None
    model: str | None = None
    description: str = ""
    faces: list[FakeFace] = field(default_factory=list)
    tags: set[str] = field(default_factory=set)
    time_zone: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    type: str = "IMAGE"
    visibility: str = "timeline"

    @property
    def file_created_at(self) -> str:
        """The instant Immich stores: from the EXIF date with its zone, else the clock time taken as UTC."""
        if self.exif_date:
            dt = datetime.fromisoformat(self.exif_date.replace("Z", "+00:00"))
            return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        return self.local_datetime or "2026-09-01T10:00:00.000Z"


class FakeImmich:
    def __init__(self) -> None:
        self.people: dict[str, dict[str, Any]] = {}
        self.assets: dict[str, FakeAsset] = {}
        self.albums: dict[str, dict[str, Any]] = {}
        self.tags: dict[str, str] = {}  # value -> id
        self.date_updates: list[dict[str, Any]] = []
        self.original_downloads = 0
        self.reject_timezone = False
        self.api_key = "key"
        self.denied_paths: set[str] = set()  # simulate missing API key permissions
        self.location_updates: list[dict[str, Any]] = []
        # Immich clears a location of exactly (0, 0) when it processes it; set to simulate it not doing so.
        self.keep_null_island = False
        # Simulate Immich failing after this many location updates.
        self.fail_location_after: int | None = None

    # --- setup helpers ---------------------------------------------------------------------

    def add_person(self, name: str, birth: date | None) -> str:
        pid = str(uuid.uuid4())
        self.people[pid] = {"id": pid, "name": name, "birthDate": birth.isoformat() if birth else None,
                            "isHidden": False, "thumbnailPath": ""}
        return pid

    def add_asset(self, file_name: str, taken: date, people: list[str | None], *, album: str | None = None,
                  face_px: int = 80, exif: bool = False, make: str | None = None, model: str | None = None) -> str:
        aid = str(uuid.uuid4())
        faces = []
        for i, pid in enumerate(people):
            x = 20 + i * (face_px + 20)
            faces.append(FakeFace(str(uuid.uuid4()), pid, (x, 50, x + face_px, 50 + face_px)))
        self.assets[aid] = FakeAsset(
            aid, file_name, taken,
            local_datetime="2026-09-01T10:00:00.000Z",
            exif_date=f"{taken.isoformat()}T12:00:00.000Z" if exif else None,
            make=make, model=model, faces=faces,
        )
        if album:
            self.albums.setdefault(album, {"id": album, "albumName": f"Album {album}", "assets": []})["assets"].append(aid)
        return aid

    def add_photo(self, file_name: str, local: str, *, tz: str | None = None, lat: float | None = None,
                  lon: float | None = None, make: str = "Canon", model: str = "Canon EOS 80D",
                  album: str | None = None) -> str:
        """A camera photo: `local` is the camera's clock time ("2024-07-01T10:00:00"); with `tz`
        (e.g. "+02:00" offset in the file and "Europe/Berlin" as Immich's zone) the EXIF date has an offset."""
        aid = self.add_asset(file_name, date.fromisoformat(local[:10]), [], album=album, make=make, model=model)
        a = self.assets[aid]
        a.local_datetime = local + ".000Z"
        if tz:
            offset = datetime.fromisoformat(local).replace(tzinfo=ZoneInfo(tz)).strftime("%z")
            a.exif_date = f"{local}.000{offset[:3]}:{offset[3:]}"
            a.time_zone = tz
        a.latitude, a.longitude = lat, lon
        return aid

    def true_age(self, asset: FakeAsset, person_id: str) -> float:
        birth = date.fromisoformat(self.people[person_id]["birthDate"])
        return years_between(birth, asset.taken)

    def image(self, asset: FakeAsset, scale: int) -> bytes:
        arr = np.full((PREVIEW_H * scale, PREVIEW_W * scale, 3), 255, dtype=np.uint8)
        for f in asset.faces:
            if f.person_id is None or self.people[f.person_id]["birthDate"] is None:
                level = 250
            else:
                level = int(round(max(0.0, self.true_age(asset, f.person_id)) * 2))
            x1, y1, x2, y2 = (v * scale for v in f.box)
            arr[y1:y2, x1:x2] = level
        buf = io.BytesIO()
        Image.fromarray(arr).save(buf, format="PNG")
        return buf.getvalue()

    # --- API ------------------------------------------------------------------------------

    def asset_json(self, a: FakeAsset) -> dict[str, Any]:
        people = {f.person_id for f in a.faces if f.person_id}
        return {
            "id": a.id, "originalFileName": a.file_name, "type": a.type, "localDateTime": a.local_datetime,
            "fileCreatedAt": a.file_created_at, "visibility": a.visibility,
            "exifInfo": {"dateTimeOriginal": a.exif_date, "make": a.make, "model": a.model,
                         "description": a.description, "timeZone": a.time_zone,
                         "latitude": a.latitude, "longitude": a.longitude},
            "people": [self.people[p] for p in sorted(people)],
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api")
        method = request.method
        body = json.loads(request.content) if request.content else None
        q = request.url.params

        if request.headers.get("x-api-key") != self.api_key:
            return httpx.Response(401, json={"message": "Invalid API key"})
        if path in self.denied_paths:
            return httpx.Response(403, json={"message": "Missing required permission"})
        if path == "/server/version":
            return httpx.Response(200, json={"major": 3, "minor": 2, "patch": 0})
        if path == "/albums" and method == "GET":
            return httpx.Response(200, json=[
                {"id": a["id"], "albumName": a["albumName"], "assetCount": len(a["assets"]),
                 "albumThumbnailAssetId": a["assets"][0] if a["assets"] else None}
                for a in self.albums.values()])
        if (m := re.fullmatch(r"/albums/([^/]+)/assets", path)) and method == "DELETE":
            album = self.albums[m[1]]
            out = []
            for aid in body["ids"]:
                if aid in album["assets"]:
                    album["assets"].remove(aid)
                    out.append({"id": aid, "success": True})
                else:
                    out.append({"id": aid, "success": False, "error": "not_found"})
            return httpx.Response(200, json=out)
        if m := re.fullmatch(r"/albums/([^/]+)", path):
            album = self.albums.get(m[1])
            return httpx.Response(200, json={**album, "assetCount": len(album["assets"])}) if album \
                else httpx.Response(404, json={"message": "not found"})
        if path == "/search/metadata":
            if "albumIds" in body:
                ids = [i for alb in body["albumIds"] for i in self.albums[alb]["assets"]]
            elif "personIds" in body:
                ids = [a.id for a in self.assets.values()
                       if any(f.person_id in body["personIds"] for f in a.faces)]
            else:
                ids = list(self.assets)
            if "takenAfter" in body:
                lo, hi = (datetime.fromisoformat(body[k].replace("Z", "+00:00")) for k in ("takenAfter", "takenBefore"))
                ids = [i for i in ids
                       if lo <= datetime.fromisoformat(self.assets[i].file_created_at.replace("Z", "+00:00")) <= hi]
            size, page = body.get("size", 250), body.get("page", 1)
            chunk = ids[(page - 1) * size: page * size]
            has_next = page * size < len(ids)
            return httpx.Response(200, json={"albums": {}, "assets": {
                "items": [self.asset_json(self.assets[i]) for i in chunk], "total": len(ids),
                "count": len(chunk), "nextPage": str(page + 1) if has_next else None}})
        if path == "/assets" and method == "PUT":
            if "latitude" in body:
                if self.fail_location_after is not None and len(self.location_updates) >= self.fail_location_after:
                    return httpx.Response(500, json={"message": "Immich is unhappy"})
                self.location_updates.append(body)
                lat, lon = body["latitude"], body["longitude"]
                if lat == 0 and lon == 0 and not self.keep_null_island:
                    lat = lon = None
                for aid in body["ids"]:
                    self.assets[aid].latitude, self.assets[aid].longitude = lat, lon
                return httpx.Response(204)
            if self.reject_timezone and "timeZone" in body:
                return httpx.Response(400, json={"message": "property timeZone should not exist"})
            self.date_updates.append(body)
            for aid in body["ids"]:
                a = self.assets[aid]
                a.exif_date = body["dateTimeOriginal"]
                a.local_datetime = body["dateTimeOriginal"][:19] + ".000Z"
                a.time_zone = body.get("timeZone") or a.time_zone
            return httpx.Response(204)
        if m := re.fullmatch(r"/assets/([^/]+)", path):
            a = self.assets[m[1]]
            if method == "PUT":
                a.description = body["description"]
            return httpx.Response(200, json=self.asset_json(a))
        if m := re.fullmatch(r"/assets/([^/]+)/thumbnail", path):
            return httpx.Response(200, content=self.image(self.assets[m[1]], 1), headers={"content-type": "image/png"})
        if m := re.fullmatch(r"/assets/([^/]+)/original", path):
            self.original_downloads += 1
            return httpx.Response(200, content=self.image(self.assets[m[1]], ORIGINAL_SCALE))
        if path == "/faces":
            a = self.assets[q["id"]]
            return httpx.Response(200, json=[
                {"id": f.id, "person": self.people.get(f.person_id) if f.person_id else None,
                 "boundingBoxX1": f.box[0], "boundingBoxY1": f.box[1], "boundingBoxX2": f.box[2],
                 "boundingBoxY2": f.box[3], "imageWidth": PREVIEW_W, "imageHeight": PREVIEW_H}
                for f in a.faces])
        if path == "/people" and method == "GET":
            return httpx.Response(200, json={"people": list(self.people.values()), "total": len(self.people),
                                             "hidden": 0, "hasNextPage": False})
        if m := re.fullmatch(r"/people/([^/]+)", path):
            p = self.people[m[1]]
            if method == "PUT":
                p["birthDate"] = body["birthDate"]
            return httpx.Response(200, json=p)
        if m := re.fullmatch(r"/people/([^/]+)/thumbnail", path):
            return httpx.Response(200, content=b"img", headers={"content-type": "image/jpeg"})
        if path == "/tags" and method == "PUT":
            out = []
            for value in body["tags"]:
                tid = self.tags.setdefault(value, str(uuid.uuid4()))
                out.append({"id": tid, "value": value, "name": value.rsplit("/", 1)[-1]})
            return httpx.Response(200, json=out)
        if (m := re.fullmatch(r"/tags/([^/]+)/assets", path)) and method == "DELETE":
            value = next(v for v, i in self.tags.items() if i == m[1])
            for aid in body["ids"]:
                self.assets[aid].tags.discard(value)
            return httpx.Response(200, json=[{"id": aid, "success": True} for aid in body["ids"]])
        if path == "/tags/assets":
            value = next(v for v, i in self.tags.items() if i in body["tagIds"])
            for aid in body["assetIds"]:
                self.assets[aid].tags.add(value)
            return httpx.Response(200, json={"count": len(body["assetIds"])})
        return httpx.Response(404, json={"message": f"no fake for {method} {path}"})


class FakeEstimator:
    """Reads the age from the grey level of the crop, adding a per-person bias via a lookup."""

    face_margin = 0.0

    def __init__(self) -> None:
        self.calls = 0
        self.faces_seen = 0
        self.bias = 0.0

    def estimate(self, faces: list[Image.Image]) -> list[float]:
        self.calls += 1
        self.faces_seen += len(faces)
        return [float(np.asarray(f.convert("L")).mean()) / 2 + self.bias for f in faces]


@pytest.fixture
def fake() -> FakeImmich:
    return FakeImmich()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(immich_url="http://immich.test", immich_api_key="key", data_dir=tmp_path,
                    timezone="Europe/Berlin", min_face_px=40)


@pytest.fixture
def estimator() -> FakeEstimator:
    return FakeEstimator()


@pytest.fixture
def guesser(fake: FakeImmich, settings: Settings, estimator: FakeEstimator) -> AgeGuesser:
    client = ImmichClient(settings.immich_url, settings.immich_api_key, transport=httpx.MockTransport(fake.handler))
    return AgeGuesser(settings, client, Store(settings.data_dir / "db.sqlite"), estimator_factory=lambda s: estimator)


@pytest.fixture
def geo(guesser: AgeGuesser) -> GeoTagger:
    g = GeoTagger(guesser.settings, guesser.immich, guesser.store)
    for h in (g.history, guesser.history):
        h.clear_poll_s = h.clear_timeout_s = 0
    return g


def gpx(points: list[tuple[float, float, str | None]], name: str = "Tour") -> bytes:
    """A GPX file with the given (lat, lon, UTC time or None) track points."""
    pts = "".join(
        f'<trkpt lat="{lat}" lon="{lon}"><ele>{1000 + i}</ele>{f"<time>{t}</time>" if t else ""}</trkpt>'
        for i, (lat, lon, t) in enumerate(points))
    return (f'<?xml version="1.0" encoding="UTF-8"?><gpx version="1.1" creator="komoot" '
            f'xmlns="http://www.topografix.com/GPX/1/1"><metadata><name>{name}</name></metadata>'
            f"<trk><name>{name}</name><trkseg>{pts}</trkseg></trk></gpx>").encode()


def written_date(asset: FakeAsset) -> datetime:
    return datetime.fromisoformat(asset.exif_date)
