"""Web UI: date photos (known dates, estimates from faces, file names) and add locations from GPX tracks.
Suggestions are only written to Immich when the user accepts them."""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, AsyncIterator, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from ..config import ENV_NAMES, Settings, env_overrides
from ..dates import parse_date_spec, parse_year_bound
from ..geotag import GeoOptions, GeoTagger
from ..gpx import MAX_GPX_BYTES, GpxError
from ..immich import ImmichClient, ImmichError
from ..jobs import JobManager
from ..service import AgeGuesser, build_guesser, report_dict

log = logging.getLogger(__name__)
_HERE = Path(__file__).parent
# Sent by our own pages on every state-changing request. A custom header cannot be sent cross-site
# without a CORS preflight (which this app never allows), so other websites cannot trigger actions.
CSRF_HEADER = "x-metadatenmaster"


class KnownDateRequest(BaseModel):
    asset_ids: list[str] = Field(min_length=1)
    date: str
    keep_order: bool = True
    album_id: str | None = None
    remove_from_album: bool = False


class EstimateRequest(BaseModel):
    asset_ids: list[str] = Field(min_length=1)
    joint: bool = False
    not_before: str | None = None
    not_after: str | None = None


class ApplyRequest(BaseModel):
    asset_ids: list[str] = Field(min_length=1)
    album_id: str | None = None
    remove_from_album: bool = False


class AssetIds(BaseModel):
    asset_ids: list[str] = Field(min_length=1)


class BirthDate(BaseModel):
    birth_date: date | None


class CalibrateRequest(BaseModel):
    per_person: int = Field(default=60, ge=5, le=1000)


class ConnectionTest(BaseModel):
    immich_url: str
    immich_api_key: str = ""


class NameDateRequest(BaseModel):
    album_id: str
    asset_ids: list[str] = Field(min_length=1)


class TrackUpdate(BaseModel):
    name: str | None = None
    # Start and end of a route without times, as local times ("2024-07-01T09:30") in `timezone`.
    start: str | None = None
    end: str | None = None
    timezone: str | None = None


class GeoRequest(BaseModel):
    track_ids: list[str] | None = None
    use_tracks: bool = True
    start: str | None = None  # local times in camera_tz, e.g. "2024-07-01T08:00"
    end: str | None = None
    album_id: str | None = None
    camera_tz: str | None = None
    use_photo_tz: bool = True
    offset_s: int = Field(default=0, ge=-30 * 86400, le=30 * 86400)
    tolerance_min: float = Field(default=10, ge=0, le=24 * 60)
    max_gap_min: float = Field(default=15, ge=0, le=24 * 60)
    use_nearby: bool = True
    nearby_min: float = Field(default=10, ge=0, le=24 * 60)
    use_siblings: bool = True


class PlaceRequest(BaseModel):
    asset_ids: list[str] = Field(min_length=1)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class GeoApplyRequest(BaseModel):
    asset_ids: list[str] = Field(min_length=1)
    fix_time: bool = False


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"Unknown time zone '{name}' (use a name like Europe/Berlin).") from None


def _local(text: str | None, tz: ZoneInfo) -> datetime | None:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"Invalid date/time '{text}'.") from None
    return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt


class Runtime:
    """Holds the current settings and AgeGuesser; the setup page can swap both at runtime."""

    def __init__(self, settings: Settings, build: Callable[[Settings], AgeGuesser],
                 guesser: AgeGuesser | None = None) -> None:
        self.settings = settings
        self._build = build
        self._lock = threading.Lock()
        self.guesser = guesser if guesser is not None else (build(settings) if settings.configured else None)
        self._geo: GeoTagger | None = None

    def require(self) -> AgeGuesser:
        g = self.guesser
        if g is None:
            raise HTTPException(status_code=503, detail="Not set up yet: open the Settings page.")
        return g

    def geo(self) -> GeoTagger:
        g = self.require()
        geo = self._geo
        if geo is None or geo.immich is not g.immich:
            geo = self._geo = GeoTagger(g.settings, g.immich, g.store)
        return geo

    def reconfigure(self, settings: Settings) -> None:
        with self._lock:
            new = self._build(settings)
            old = self.guesser
            if old is not None and old.backend == new.backend:
                new.adopt_model(old)
            # The old instance is left to running jobs and the garbage collector.
            self.guesser, self.settings = new, settings


def _client(settings: Settings) -> ImmichClient:
    return ImmichClient(settings.immich_url, settings.immich_api_key, timeout=15)


def create_app(guesser: AgeGuesser | None = None, *, settings: Settings | None = None,
               build: Callable[[Settings], AgeGuesser] = build_guesser,
               make_client: Callable[[Settings], ImmichClient] = _client) -> FastAPI:
    if settings is None:
        settings = guesser.settings if guesser is not None else Settings.load()
    runtime = Runtime(settings, build, guesser)
    jobs = JobManager()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        jobs.shutdown()

    app = FastAPI(title="Immich Metadatenmaster", lifespan=lifespan)
    geo = runtime.geo
    templates = Jinja2Templates(directory=_HERE / "templates")
    app.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")
    app.state.runtime = runtime
    app.state.jobs = jobs

    @app.middleware("http")
    async def csrf_guard(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"} and request.headers.get(CSRF_HEADER) != "1":
            return JSONResponse({"detail": "Missing request header."}, status_code=403)
        return await call_next(request)

    def bad_request(exc: Exception) -> HTTPException:
        return HTTPException(status_code=400, detail=str(exc))

    def page(request: Request, name: str, **context: Any) -> Response:
        return templates.TemplateResponse(request, name, {"configured": runtime.guesser is not None,
                                                          "nav": request.url.path, **context})

    def setup_redirect() -> None:
        if runtime.guesser is None:
            raise _Redirect("/settings")

    g = runtime.require

    # --- pages ------------------------------------------------------------------------------

    def albums_or_error(guesser: AgeGuesser) -> tuple[list[dict[str, Any]], str, str]:
        try:
            version = guesser.immich.server_version()
            albums = sorted(guesser.immich.albums(), key=lambda a: a["albumName"].lower())
        except ImmichError as exc:
            return [], "", str(exc)
        return albums, version, ""

    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(setup_redirect)])
    def index(request: Request) -> Response:
        guesser = g()
        error, version = "", ""
        try:
            version = guesser.immich.server_version()
        except ImmichError as exc:
            error = str(exc)
        pending = geo().suggestions()
        batches = guesser.store.change_batches(limit=5)
        return page(request, "index.html", version=version, error=error, tracks=len(guesser.store.tracks(False)),
                    geo_pending=sum(1 for s in pending.values() if s.get("status") == "ok"),
                    batches=batches, calibration=guesser.calibration_summary())

    @app.get("/dates", response_class=HTMLResponse, dependencies=[Depends(setup_redirect)])
    def dates_page(request: Request) -> Response:
        guesser = g()
        albums, version, error = albums_or_error(guesser)
        return page(request, "dates.html", albums=albums, version=version, error=error,
                    calibration=guesser.calibration_summary())

    @app.get("/locations", response_class=HTMLResponse, dependencies=[Depends(setup_redirect)])
    def locations_page(request: Request) -> Response:
        guesser = g()
        albums, _, error = albums_or_error(guesser)
        s = guesser.settings
        return page(request, "locations.html", albums=albums, error=error, timezone=s.timezone,
                    tile_url=s.map_tile_url, attribution=s.map_attribution, immich_url=s.public_url)

    @app.get("/history", response_class=HTMLResponse, dependencies=[Depends(setup_redirect)])
    def history_page(request: Request) -> Response:
        return page(request, "history.html", immich_url=g().settings.public_url)

    @app.get("/album/{album_id}", response_class=HTMLResponse, dependencies=[Depends(setup_redirect)])
    def album_page(request: Request, album_id: str) -> Response:
        guesser = g()
        try:
            album = guesser.immich.album(album_id)
        except ImmichError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return page(request, "album.html", album=album, immich_url=guesser.settings.public_url,
                    remove_default=guesser.settings.remove_from_album)

    @app.get("/people", response_class=HTMLResponse, dependencies=[Depends(setup_redirect)])
    def people_page(request: Request) -> Response:
        return page(request, "people.html")

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request) -> Response:
        return page(request, "settings.html")

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "configured": runtime.guesser is not None}

    @app.get("/thumb/{asset_id}")
    def thumbnail(asset_id: str, size: str = "thumbnail") -> Response:
        if size not in {"thumbnail", "preview"}:
            raise HTTPException(status_code=400, detail="bad size")
        try:
            data, content_type = g().immich.thumbnail(asset_id, size)
        except ImmichError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return Response(data, media_type=content_type, headers={"Cache-Control": "private, max-age=86400"})

    # --- settings ---------------------------------------------------------------------------

    @app.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        s = runtime.settings
        values = {name: getattr(s, name) for name in ENV_NAMES if name != "immich_api_key"}
        return {"values": values, "has_api_key": bool(s.immich_api_key), "locked": sorted(env_overrides()),
                "configured": runtime.guesser is not None, "data_dir": str(s.data_dir)}

    def merged_settings(body: dict[str, Any]) -> Settings:
        current = runtime.settings
        locked = env_overrides()
        values = {k: v for k, v in body.items() if k in ENV_NAMES and k not in locked}
        new_url = str(values.get("immich_url", current.immich_url)).strip().rstrip("/")
        if not values.get("immich_api_key"):
            # Never send the saved key to a server it was not entered for.
            if new_url != current.immich_url and "immich_api_key" not in locked:
                raise ValueError("Enter the API key again when changing the Immich URL.")
            values.pop("immich_api_key", None)
        merged = current.with_values(values)
        merged.validate()
        return merged

    @app.post("/api/settings/test")
    def test_connection(body: ConnectionTest) -> dict[str, Any]:
        try:
            s = merged_settings(body.model_dump())
        except ValueError as exc:
            raise bad_request(exc) from exc
        if not s.configured:
            raise bad_request(ValueError("Enter the Immich URL and an API key."))
        return _probe(make_client(s))

    @app.put("/api/settings")
    def save_settings(body: dict[str, Any]) -> dict[str, Any]:
        try:
            s = merged_settings(body)
        except (ValueError, TypeError) as exc:
            raise bad_request(exc) from exc
        if not s.configured:
            raise bad_request(ValueError("Enter the Immich URL and an API key."))
        s.save(exclude=env_overrides())
        runtime.reconfigure(s)
        return {"ok": True}

    # --- data -------------------------------------------------------------------------------

    @app.get("/api/albums/{album_id}/assets")
    def album_assets(album_id: str) -> dict[str, Any]:
        guesser = g()
        try:
            assets = guesser.immich.album_assets(album_id)
        except ImmichError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        ids = [a.id for a in assets]
        dated = guesser.store.dated(ids)
        estimates = guesser.store.estimates(ids)
        name_dates = guesser.store.suggestions("name_date", ids)
        people: dict[str, dict[str, Any]] = {}
        items = []
        for a in assets:
            for p in a.people:
                if p.name:
                    entry = people.setdefault(p.id, {"id": p.id, "name": p.name, "photos": 0,
                                                     "birth_date": p.birth_date.isoformat() if p.birth_date else None})
                    entry["photos"] += 1
            items.append({
                "id": a.id,
                "file_name": a.file_name,
                "date": a.local_datetime.isoformat() if a.local_datetime else None,
                "people": [p.name for p in a.people if p.name],
                "dated": dated[a.id].to_dict() if a.id in dated else None,
                "estimate": estimates.get(a.id),
                "name_date": name_dates.get(a.id),
            })
        return {"assets": items, "people": sorted(people.values(), key=lambda p: p["name"].lower())}

    @app.get("/api/people")
    def list_people() -> list[dict[str, Any]]:
        try:
            people = [p for p in g().immich.people() if p.name]
        except ImmichError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return [{"id": p.id, "name": p.name, "hidden": p.is_hidden,
                 "birth_date": p.birth_date.isoformat() if p.birth_date else None}
                for p in sorted(people, key=lambda p: p.name.lower())]

    @app.get("/api/people/{person_id}/thumbnail")
    def person_thumbnail(person_id: str) -> Response:
        try:
            data, content_type = g().immich.person_thumbnail(person_id)
        except ImmichError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return Response(data, media_type=content_type, headers={"Cache-Control": "private, max-age=3600"})

    @app.put("/api/people/{person_id}/birthdate")
    def set_birthdate(person_id: str, body: BirthDate) -> dict[str, Any]:
        if body.birth_date and body.birth_date > date.today():
            raise HTTPException(status_code=400, detail="The birthday is in the future.")
        try:
            p = g().immich.set_birth_date(person_id, body.birth_date)
        except ImmichError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"id": p.id, "name": p.name, "birth_date": p.birth_date.isoformat() if p.birth_date else None}

    @app.get("/api/parse-date")
    def parse_date(text: str) -> dict[str, Any]:
        try:
            spec = parse_date_spec(text)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "label": spec.label, "precision": spec.precision,
                "written": spec.anchor(runtime.settings.date_anchor).isoformat()}

    # --- actions (background jobs) ----------------------------------------------------------

    @app.post("/api/known-date")
    def known_date(body: KnownDateRequest) -> dict[str, Any]:
        guesser = g()
        try:
            spec = parse_date_spec(body.date)
        except ValueError as exc:
            raise bad_request(exc) from exc
        album = body.album_id if body.remove_from_album else None
        job = jobs.submit("known-date", lambda progress: guesser.apply_known_date(
            body.asset_ids, spec, keep_order=body.keep_order, remove_from_album=album, progress=progress))
        return job.to_dict()

    @app.post("/api/estimate")
    def estimate(body: EstimateRequest) -> dict[str, Any]:
        guesser = g()
        try:
            not_before = parse_year_bound(body.not_before, end=False)
            not_after = parse_year_bound(body.not_after, end=True)
        except ValueError as exc:
            raise bad_request(exc) from exc
        job = jobs.submit("estimate", lambda progress: guesser.estimate(
            body.asset_ids, joint=body.joint, not_before=not_before, not_after=not_after, progress=progress))
        return job.to_dict()

    @app.post("/api/name-dates")
    def name_dates(body: NameDateRequest) -> dict[str, Any]:
        guesser = g()
        wanted = set(body.asset_ids)

        def run(progress: Callable[[int, int], None]) -> dict[str, Any]:
            assets = [a for a in guesser.immich.album_assets(body.album_id) if a.id in wanted]
            return guesser.suggest_name_dates(assets, progress=progress)

        return jobs.submit("name-dates", run).to_dict()

    @app.post("/api/apply-estimates")
    def apply_estimates(body: ApplyRequest) -> dict[str, Any]:
        """Write the suggestions of the given photos: the date from the file name where there is one,
        else the estimate from faces."""
        guesser = g()
        album = body.album_id if body.remove_from_album else None
        by_name = set(guesser.store.suggestions("name_date", body.asset_ids))

        def run(progress: Callable[[int, int], None]) -> dict[str, Any]:
            rest = [aid for aid in body.asset_ids if aid not in by_name]
            if not by_name:
                return guesser.apply_estimates(rest, remove_from_album=album, progress=progress)
            first = guesser.apply_name_dates([a for a in body.asset_ids if a in by_name],
                                             remove_from_album=album, progress=progress)
            if not rest:
                return first
            second = guesser.apply_estimates(rest, remove_from_album=album, progress=progress)
            merged = {k: first.get(k, 0) + second.get(k, 0) for k in ("written", "skipped")}
            if "removed" in first or "removed" in second:
                merged["removed"] = first.get("removed", 0) + second.get("removed", 0)
            if "remove_error" in first or "remove_error" in second:
                merged["remove_error"] = first.get("remove_error") or second.get("remove_error")
            return merged

        return jobs.submit("apply", run).to_dict()

    @app.post("/api/discard-estimates")
    def discard_estimates(body: AssetIds) -> dict[str, Any]:
        guesser = g()
        guesser.store.delete_estimates(body.asset_ids)
        guesser.store.delete_suggestions("name_date", body.asset_ids)
        return {"discarded": len(body.asset_ids)}

    @app.post("/api/calibrate")
    def calibrate(body: CalibrateRequest) -> dict[str, Any]:
        guesser = g()
        job = jobs.submit("calibrate", lambda progress: report_dict(
            guesser.calibrate(per_person=body.per_person, progress=progress)))
        return job.to_dict()

    # --- locations ----------------------------------------------------------------------------

    @app.get("/api/tracks")
    def list_tracks() -> list[dict[str, Any]]:
        return geo().tracks()

    @app.post("/api/tracks")
    async def upload_track(request: Request, file_name: str = "track.gpx") -> dict[str, Any]:
        tagger = geo()
        data = await request.body()
        if len(data) > MAX_GPX_BYTES:
            raise HTTPException(status_code=413, detail="The file is too large (more than 50 MB).")
        try:
            return await run_in_threadpool(tagger.add_track, file_name[:200], data)
        except GpxError as exc:
            raise bad_request(exc) from exc

    @app.put("/api/tracks/{track_id}")
    def update_track(track_id: str, body: TrackUpdate) -> dict[str, Any]:
        tagger = geo()
        try:
            if body.name is not None:
                tagger.rename_track(track_id, body.name)
            if body.start or body.end:
                tz = _zone(body.timezone or tagger.settings.timezone)
                start, end = _local(body.start, tz), _local(body.end, tz)
                if start is None or end is None:
                    raise ValueError("Enter both the start and the end time.")
                tagger.set_track_times(track_id, start, end)
            return tagger.track_summary(track_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Unknown track") from None
        except ValueError as exc:
            raise bad_request(exc) from exc

    @app.delete("/api/tracks/{track_id}")
    def delete_track(track_id: str) -> dict[str, Any]:
        geo().delete_track(track_id)
        return {"deleted": track_id}

    @app.post("/api/geo/find")
    def geo_find(body: GeoRequest) -> dict[str, Any]:
        tagger = geo()
        try:
            tz_name = body.camera_tz or tagger.settings.timezone
            tz = _zone(tz_name)
            opts = GeoOptions(
                track_ids=body.track_ids, use_tracks=body.use_tracks, start=_local(body.start, tz),
                end=_local(body.end, tz), album_id=body.album_id or None, camera_tz=tz_name,
                use_photo_tz=body.use_photo_tz, offset_s=body.offset_s, tolerance_min=body.tolerance_min,
                max_gap_min=body.max_gap_min, use_nearby=body.use_nearby, nearby_min=body.nearby_min,
                use_siblings=body.use_siblings)
        except ValueError as exc:
            raise bad_request(exc) from exc
        if bool(opts.start) != bool(opts.end):
            raise bad_request(ValueError("Enter both ends of the time range, or neither."))
        return jobs.submit("geo-find", lambda progress: tagger.find(opts, progress=progress)).to_dict()

    @app.get("/api/geo/suggestions")
    def geo_suggestions() -> dict[str, Any]:
        tagger = geo()
        items = [{"id": aid, **data} for aid, data in tagger.suggestions().items()]
        items.sort(key=lambda x: x.get("instant") or "")
        return {"run": tagger.run(), "items": items}

    @app.post("/api/geo/place")
    def geo_place(body: PlaceRequest) -> dict[str, Any]:
        return {"placed": geo().place(body.asset_ids, body.lat, body.lon)}

    @app.post("/api/geo/discard")
    def geo_discard(body: AssetIds) -> dict[str, Any]:
        geo().discard(body.asset_ids)
        return {"discarded": len(body.asset_ids)}

    @app.post("/api/geo/apply")
    def geo_apply(body: GeoApplyRequest) -> dict[str, Any]:
        tagger = geo()
        return jobs.submit("geo-apply", lambda progress: tagger.apply(
            body.asset_ids, fix_time=body.fix_time, progress=progress)).to_dict()

    # --- history ----------------------------------------------------------------------------

    @app.get("/api/history")
    def history() -> list[dict[str, Any]]:
        guesser = g()
        out = []
        for b in guesser.store.change_batches():
            full = guesser.store.change_batch(b["id"]) or {"changes": []}
            out.append({**b, "undoable": guesser.history.undoable({**b, "changes": full["changes"]})})
        return out

    @app.get("/api/history/{batch_id}")
    def history_batch(batch_id: int) -> dict[str, Any]:
        guesser = g()
        batch = guesser.store.change_batch(batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail="Unknown change")
        return {**batch, "undoable": guesser.history.undoable(batch)}

    @app.post("/api/history/{batch_id}/undo")
    def undo(batch_id: int) -> dict[str, Any]:
        guesser = g()
        batch = guesser.store.change_batch(batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail="Unknown change")
        if not guesser.history.undoable(batch):
            raise bad_request(ValueError("This change cannot be undone."))
        return jobs.submit("undo", lambda progress: guesser.history.undo(batch_id, progress=progress)).to_dict()

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str) -> dict[str, Any]:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Unknown job")
        return job.to_dict()

    @app.exception_handler(_Redirect)
    async def redirect(_: Request, exc: _Redirect) -> Response:
        return RedirectResponse(exc.location, status_code=303)

    return app


class _Redirect(Exception):
    def __init__(self, location: str) -> None:
        self.location = location


def _probe(client: ImmichClient) -> dict[str, Any]:
    """Check that Immich is reachable and the key can read what the tool needs."""
    checks: list[dict[str, Any]] = []
    try:
        try:
            version = client.server_version()
        except ImmichError as exc:
            return {"ok": False, "checks": [{"label": "Reach Immich", "ok": False, "detail": str(exc)}]}
        checks.append({"label": f"Reach Immich {version}", "ok": True, "detail": ""})
        for label, fn in [
            ("Read albums (album.read)", client.albums),
            ("Read people (person.read)", lambda: client._json("GET", "/people", params={"size": 1})),
            ("Read photos (asset.read)", lambda: client._json("POST", "/search/metadata", json={"size": 1})),
        ]:
            try:
                fn()
                checks.append({"label": label, "ok": True, "detail": ""})
            except ImmichError as exc:
                detail = "the API key lacks this permission" if " 403:" in str(exc) else \
                    "wrong API key" if " 401:" in str(exc) else str(exc)
                checks.append({"label": label, "ok": False, "detail": detail})
    finally:
        client.close()
    return {"ok": all(c["ok"] for c in checks), "checks": checks}
