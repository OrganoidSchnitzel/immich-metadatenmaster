"""Web UI: date photos (known dates, estimates from faces, file names) and add locations from GPX tracks.
Suggestions are only written to Immich when the user accepts them."""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any, AsyncIterator, Callable
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .. import users
from ..auth import COOKIE, MIN_LENGTH, SESSION_S, Auth, LoginThrottle, hash_password, password_state, \
    verify_password
from ..config import ENV_NAMES, ENV_ONLY, Settings, env_overrides
from ..dates import parse_date_spec, parse_year_bound
from ..geocode import GeocodeError, reverse as reverse_geocode, search as geocode_search
from ..geotag import GeoOptions, GeoTagger, lacks_zone, place_zone
from ..gpx import MAX_GPX_BYTES, GpxError
from ..immich import Asset, ImmichClient, ImmichError
from ..jobs import Job, JobManager
from ..overview import LibraryScan, is_list, list_title
from ..service import AgeGuesser, SharedModels, build_guesser, report_dict
from ..users import LoginFailed, Sessions, User
from ..versions import VersionDates

log = logging.getLogger(__name__)
_HERE = Path(__file__).parent
# Sent by our own pages on every state-changing request. A custom header cannot be sent cross-site
# without a CORS preflight (which this app never allows), so other websites cannot trigger actions.
CSRF_HEADER = "x-metadatenmaster"
# Reachable without logging in (when a password is set).
PUBLIC_PATHS = {"/login", "/api/login", "/healthz"}
# A wrong password costs this long, on top of the throttle.
FAILED_LOGIN_DELAY_S = 1.0
# Who is logged in, for the request being handled (with several users).
_user: ContextVar[User | None] = ContextVar("user", default=None)


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
    album_id: str | None = None  # only to find the job again from the album page


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
    include_located: bool = False
    camera_offsets: dict[str, int] = Field(default_factory=dict)


class PlaceRequest(BaseModel):
    asset_ids: list[str] = Field(min_length=1)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    label: str = ""


class VersionFindRequest(BaseModel):
    album_id: str | None = None


class PlaceNamesRequest(BaseModel):
    keys: list[str] = Field(max_length=5000)  # "47.012,11.007": positions rounded to 3 decimals (~100 m)
    lang: str | None = None


_PLACE_KEY = re.compile(r"^-?\d{1,2}\.\d{3},-?\d{1,3}\.\d{3}$")
# New names looked up per request; the page asks again for the rest (go easy on the public geocoder).
PLACE_BATCH = 15


class LoginRequest(BaseModel):
    password: str = Field(default="", max_length=1000)
    # With several users: the Immich account, or an Immich API key instead.
    email: str = Field(default="", max_length=320)
    api_key: str = Field(default="", max_length=1000)


class PasswordChange(BaseModel):
    current: str = Field(default="", max_length=1000)
    new: str = Field(default="", max_length=1000)  # empty: remove the password


class GeoApplyRequest(BaseModel):
    asset_ids: list[str] = Field(min_length=1)
    fix_time: bool = False
    local_time: bool = False


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


def safe_next(target: str | None) -> str:
    """Where to go after logging in: only paths on this site."""
    if not target or not target.startswith("/") or target.startswith(("//", "/\\", "/login")):
        return "/"
    return target


def local_time(value: str | None, tz_name: str) -> str:
    """"2026-10-21T11:58:00+00:00" → "2026-10-21 13:58" in the given zone. Old records without an offset
    were written in this machine's local time."""
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value)
        zone = ZoneInfo(tz_name)
    except (ValueError, ZoneInfoNotFoundError):
        return value.replace("T", " ")[:16]
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(zone).strftime("%Y-%m-%d %H:%M")


@dataclass
class Workspace:
    """What one person works with: their Immich connection and their own local data."""

    owner: str  # their Immich user id ("" with a single user)
    guesser: AgeGuesser
    geo: GeoTagger
    versions: VersionDates
    library: LibraryScan

    @classmethod
    def of(cls, owner: str, g: AgeGuesser) -> "Workspace":
        return cls(owner, g, GeoTagger(g.settings, g.immich, g.store), VersionDates(g.settings, g.immich, g.store),
                   LibraryScan(g.immich, g.store))


def _use(client: ImmichClient, kind: str, secret: str) -> ImmichClient:
    client.set_credentials(**{"access_token" if kind == "token" else "api_key": secret})
    return client


class Runtime:
    """Holds the current settings and the workspaces (one, or one per Immich user); the settings page
    can change both at runtime."""

    def __init__(self, settings: Settings, build: Callable[..., AgeGuesser],
                 make_client: Callable[[Settings], ImmichClient], guesser: AgeGuesser | None = None) -> None:
        self.settings = settings
        self._build = build
        self._make_client = make_client
        self._lock = threading.Lock()
        self.models = SharedModels()
        if guesser is None and settings.configured and not settings.multi_user:
            guesser = self.models.share(build(settings))
        self.guesser = guesser  # with a single user
        self._single: Workspace | None = None
        self._users: dict[str, Workspace] = {}

    @property
    def multi(self) -> bool:
        return self.settings.multi_user

    @property
    def ready(self) -> bool:
        return self.settings.configured if self.multi else self.guesser is not None

    def workspace(self, user: User | None) -> Workspace:
        if not self.ready:
            raise HTTPException(status_code=503, detail="Not set up yet: open the Settings page.")
        if not self.multi:
            g = self.guesser
            ws = self._single
            if ws is None or ws.guesser is not g:
                ws = self._single = Workspace.of("", g)  # type: ignore[arg-type]
            return ws
        if user is None:
            raise HTTPException(status_code=401, detail="Log in first.")
        with self._lock:
            ws = self._users.get(user.id)
            if ws is None:
                s = replace(self.settings, data_dir=users.user_dir(self.settings.data_dir, user.id))
                g = self.models.share(self._build(s, _use(self._make_client(s), user.kind, user.secret)))
                ws = self._users[user.id] = Workspace.of(user.id, g)
        if ws.guesser.immich.credentials != (user.kind, user.secret):
            # The same person in another browser: carry on with the session of the latest request.
            _use(ws.guesser.immich, user.kind, user.secret)
        return ws

    def client(self, kind: str = "", secret: str = "") -> ImmichClient:
        """A new connection to Immich with the given credentials (none: for logging in)."""
        return _use(self._make_client(replace(self.settings, immich_api_key="")), kind, secret)

    def session_ended(self, user: User, sessions: Sessions) -> None:
        """A session was ended: its workspace carries on with another session of the same person."""
        with self._lock:
            ws = self._users.get(user.id)
            if ws is None or ws.guesser.immich.credentials != (user.kind, user.secret):
                return
            other = sessions.credentials(self.settings.immich_url, user.id)
            if other:
                _use(ws.guesser.immich, *other)
            else:
                del self._users[user.id]

    def claim_single_user_data(self, user: dict[str, Any] | None = None) -> None:
        """The data of the single-user setup goes to the owner of its API key, or else to the first
        Immich admin who logs in."""
        data_dir = self.settings.data_dir
        if users.owner(data_dir) or not users.has_single_user_data(data_dir):
            return
        who = None
        if self.settings.immich_api_key:
            client = self._make_client(self.settings)
            try:
                who = client.me()
            except ImmichError as exc:
                log.info("Cannot tell whose API key is configured: %s", exc)
            finally:
                client.close()
        if who is None and user is not None and user["is_admin"]:
            who = user
        if who is not None and not (data_dir / "users" / who["id"]).exists() and users.claim(data_dir, who):
            log.info("The data of the single-user setup now belongs to %s", who.get("email") or who["id"])
            with self._lock:
                self._users.pop(who["id"], None)

    def set_values(self, values: dict[str, Any]) -> Settings:
        """Change and save settings that need no new connection to Immich (e.g. the password)."""
        with self._lock:
            updated = self.settings.with_values(values)
            updated.save(exclude=env_overrides())
            self.settings = updated
        return updated

    def reconfigure(self, settings: Settings) -> None:
        new = None if settings.multi_user else self.models.share(self._build(settings))
        with self._lock:
            old = self.guesser
            if new is not None and old is not None and old.backend == new.backend:
                new.adopt_model(old)
            self.models.keep_only(settings)
            # Old instances are left to running jobs and the garbage collector.
            self.guesser, self.settings = new, settings
            self._users.clear()


def _client(settings: Settings) -> ImmichClient:
    return ImmichClient(settings.immich_url, settings.immich_api_key, timeout=30)


def create_app(guesser: AgeGuesser | None = None, *, settings: Settings | None = None,
               build: Callable[..., AgeGuesser] = build_guesser,
               make_client: Callable[[Settings], ImmichClient] = _client,
               geocode_transport: Any = None) -> FastAPI:
    if settings is None:
        settings = guesser.settings if guesser is not None else Settings.load()
    runtime = Runtime(settings, build, make_client, guesser)
    jobs = JobManager()
    sessions = Sessions(settings.data_dir)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        jobs.shutdown()

    app = FastAPI(title="Immich Metadatenmaster", lifespan=lifespan)

    def workspace() -> Workspace:
        return runtime.workspace(_user.get())

    def g() -> AgeGuesser:
        return workspace().guesser

    def geo() -> GeoTagger:
        return workspace().geo

    def versions() -> VersionDates:
        return workspace().versions

    def library() -> LibraryScan:
        return workspace().library

    def owner() -> str:
        user = _user.get()
        return user.id if user is not None else ""

    def submit(kind: str, fn: Callable[[Callable[[int, int], None]], Any], **kwargs: Any) -> Job:
        """Start a background job of the person logged in."""
        return jobs.submit(kind, fn, owner=owner(), **kwargs)

    templates = Jinja2Templates(directory=_HERE / "templates")
    templates.env.filters["local_time"] = lambda value: local_time(value, runtime.settings.timezone)
    app.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")
    app.state.runtime = runtime
    app.state.jobs = jobs

    @app.middleware("http")
    async def csrf_guard(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"} and request.headers.get(CSRF_HEADER) != "1":
            return JSONResponse({"detail": "Missing request header."}, status_code=403)
        return await call_next(request)

    auth = Auth(settings.data_dir)
    throttle = LoginThrottle()

    def needs_login(request: Request) -> Response:
        path = request.url.path
        if path.startswith(("/api/", "/thumb/")):
            return JSONResponse({"detail": "Log in first.", "login": True}, status_code=401)
        target = path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=303)

    def end_session(user: User) -> None:
        sessions.delete(user.session)
        runtime.session_ended(user, sessions)

    async def logged_in(request: Request) -> User | None:
        """The person this browser is logged in as (checked with Immich every few minutes)."""
        user = sessions.get(runtime.settings.immich_url, request.cookies.get(COOKIE))
        if user is None or time.time() - user.checked < users.RECHECK_S:
            return user
        client = runtime.client(user.kind, user.secret)
        try:
            info = await run_in_threadpool(users.check, client)
        except ImmichError as exc:  # Immich is down: nobody is logged out for that
            log.info("Cannot check the session of %s: %s", user.email, exc)
            return user
        finally:
            client.close()
        if info is None or info["id"] != user.id:
            log.info("Immich no longer accepts the session of %s", user.email)
            end_session(user)
            return None
        sessions.update(user, info)
        return user

    @app.middleware("http")
    async def login_guard(request: Request, call_next):
        """With a password set, or with several users, everything but the login page needs a session."""
        path = request.url.path
        public = path in PUBLIC_PATHS or path.startswith("/static/")
        if runtime.multi:
            if not runtime.settings.configured:  # first setup: the settings page is open
                return await call_next(request)
            user = await logged_in(request)
            if user is None and not public:
                return needs_login(request)
            token = _user.set(user)
            try:
                return await call_next(request)
            finally:
                _user.reset(token)
        if Auth.enabled(runtime.settings) and not public \
                and not auth.valid(runtime.settings, request.cookies.get(COOKIE)):
            return needs_login(request)
        return await call_next(request)

    def set_cookie(response: Response, request: Request, value: str) -> None:
        proto = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
        response.set_cookie(COOKIE, value, max_age=SESSION_S, httponly=True, samesite="lax",
                            secure=proto == "https", path="/")

    def start_session(response: Response, request: Request, s: Settings) -> None:
        set_cookie(response, request, auth.issue(s))

    def require_admin() -> None:
        """With several users, only Immich admins change the settings."""
        if runtime.multi and runtime.settings.configured:
            user = _user.get()
            if user is None or not user.is_admin:
                raise HTTPException(status_code=403, detail="Only Immich administrators can change the settings.")

    def bad_request(exc: Exception) -> HTTPException:
        return HTTPException(status_code=400, detail=str(exc))

    def page(request: Request, name: str, **context: Any) -> Response:
        multi = runtime.multi and runtime.settings.configured
        return templates.TemplateResponse(request, name, {
            "configured": runtime.ready, "nav": request.url.path, "multi": multi, "user": _user.get(),
            "logout_on": multi or (not runtime.multi and Auth.enabled(runtime.settings)), **context})

    def setup_redirect() -> None:
        if not runtime.ready:
            raise _Redirect("/settings")

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
                    calibration=guesser.calibration_summary(), immich_url=guesser.settings.public_url)

    @app.get("/locations", response_class=HTMLResponse, dependencies=[Depends(setup_redirect)])
    def locations_page(request: Request) -> Response:
        guesser = g()
        albums, _, error = albums_or_error(guesser)
        s = guesser.settings
        return page(request, "locations.html", albums=albums, error=error, timezone=s.timezone,
                    tile_url=s.map_tile_url, attribution=s.map_attribution, immich_url=s.public_url,
                    geocoder=bool(s.geocoder_url), place_names=bool(s.geocoder_url and s.place_names))

    @app.get("/history", response_class=HTMLResponse, dependencies=[Depends(setup_redirect)])
    def history_page(request: Request) -> Response:
        return page(request, "history.html", immich_url=g().settings.public_url)

    @app.get("/album/{album_id}", response_class=HTMLResponse, dependencies=[Depends(setup_redirect)])
    def album_page(request: Request, album_id: str) -> Response:
        guesser = g()
        try:
            # A list from the library overview ("scan:no-date:2019-07") works like an album.
            album = {"id": album_id, "albumName": list_title(album_id)} if is_list(album_id) \
                else guesser.immich.album(album_id)
        except (ImmichError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return page(request, "album.html", album=album, immich_url=guesser.settings.public_url,
                    remove_default=guesser.settings.remove_from_album, is_list=is_list(album_id),
                    list_kind=album_id.split(":")[1] if is_list(album_id) else "")

    @app.get("/people", response_class=HTMLResponse, dependencies=[Depends(setup_redirect)])
    def people_page(request: Request) -> Response:
        return page(request, "people.html")

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request) -> Response:
        return page(request, "settings.html")

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, next: str = "/") -> Response:
        if not (runtime.multi and runtime.settings.configured) and not Auth.enabled(runtime.settings):
            return RedirectResponse("/", status_code=303)
        return page(request, "login.html", next=safe_next(next), bare=True)

    async def immich_login(request: Request, body: LoginRequest) -> Response:
        """Log in with an Immich account (email and password, or an API key)."""
        if throttle.blocked():
            raise HTTPException(status_code=429, detail="Too many failed logins. Try again in a few minutes.")
        if not body.api_key.strip() and not (body.email.strip() and body.password):
            raise bad_request(ValueError("Enter your email and password."))
        client = runtime.client()
        try:
            info, kind, secret = await run_in_threadpool(users.log_in, client, email=body.email.strip(),
                                                         password=body.password, api_key=body.api_key.strip())
        except LoginFailed as exc:
            throttle.failed()
            await asyncio.sleep(FAILED_LOGIN_DELAY_S)
            raise HTTPException(status_code=401, detail=str(exc)) from None
        except ImmichError as exc:
            raise HTTPException(status_code=502, detail=f"Immich cannot be reached: {exc}") from None
        finally:
            client.close()
        await run_in_threadpool(runtime.claim_single_user_data, info)
        response = JSONResponse({"ok": True, "name": info["name"]})
        set_cookie(response, request, sessions.create(runtime.settings.immich_url, info, kind, secret))
        return response

    @app.post("/api/login")
    async def login(request: Request, body: LoginRequest) -> Response:
        s = runtime.settings
        if runtime.multi and s.configured:
            return await immich_login(request, body)
        if runtime.multi or not Auth.enabled(s):
            return JSONResponse({"ok": True})
        if throttle.blocked():
            raise HTTPException(status_code=429, detail="Too many wrong passwords. Try again in a few minutes.")
        if not await run_in_threadpool(auth.check, s, body.password):  # scrypt takes a moment
            throttle.failed()
            await asyncio.sleep(FAILED_LOGIN_DELAY_S)
            raise HTTPException(status_code=401, detail="Wrong password.")
        response = JSONResponse({"ok": True})
        start_session(response, request, s)
        return response

    @app.post("/api/logout")
    def logout() -> Response:
        user = _user.get()
        if user is not None:
            end_session(user)
            if user.kind == "token":  # end the Immich session this login started
                client = runtime.client(user.kind, user.secret)
                try:
                    client.logout()
                except ImmichError as exc:
                    log.info("Immich logout failed: %s", exc)
                finally:
                    client.close()
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/")
        return response

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "configured": runtime.ready}

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
        user = _user.get()
        values = {name: getattr(s, name) for name in ENV_NAMES if name != "immich_api_key" and name not in ENV_ONLY}
        return {"values": values, "has_api_key": bool(s.immich_api_key), "locked": sorted(env_overrides()),
                "configured": runtime.ready, "data_dir": str(s.data_dir),
                "password": password_state(s), "password_min_length": MIN_LENGTH,
                "can_edit": not (runtime.multi and s.configured) or bool(user and user.is_admin),
                "user": {"name": user.name, "email": user.email, "is_admin": user.is_admin,
                         "login": "API key" if user.kind == "api_key" else "password"} if user else None}

    @app.put("/api/settings/password", dependencies=[Depends(require_admin)])
    async def change_password(request: Request, body: PasswordChange) -> Response:
        s = runtime.settings
        state = password_state(s)
        if state == "env":
            raise bad_request(ValueError("The password is set by the UI_PASSWORD environment variable."))
        if state == "set" and not await run_in_threadpool(verify_password, s.ui_password_hash, body.current):
            await asyncio.sleep(FAILED_LOGIN_DELAY_S)
            raise bad_request(ValueError("The current password is wrong."))
        if body.new and len(body.new) < MIN_LENGTH:
            raise bad_request(ValueError(f"Use at least {MIN_LENGTH} characters."))
        new_hash = await run_in_threadpool(hash_password, body.new) if body.new else ""
        updated = runtime.set_values({"ui_password_hash": new_hash})
        response = JSONResponse({"ok": True, "password": password_state(updated)})
        if body.new:  # this browser stays logged in; all others are logged out
            start_session(response, request, updated)
        return response

    def merged_settings(body: dict[str, Any]) -> Settings:
        current = runtime.settings
        locked = env_overrides()
        values = {k: v for k, v in body.items() if k in ENV_NAMES and k not in locked and k not in ENV_ONLY}
        new_url = str(values.get("immich_url", current.immich_url)).strip().rstrip("/")
        if not values.get("immich_api_key"):
            values.pop("immich_api_key", None)
            # Never send the saved key to a server it was not entered for.
            if new_url != current.immich_url and "immich_api_key" not in locked:
                if not current.with_values(values).multi_user:
                    raise ValueError("Enter the API key again when changing the Immich URL.")
                values["immich_api_key"] = ""  # not needed with several users
        merged = current.with_values(values)
        merged.validate()
        return merged

    def missing(s: Settings) -> ValueError:
        return ValueError("Enter the Immich URL." if s.multi_user else "Enter the Immich URL and an API key.")

    @app.post("/api/settings/test", dependencies=[Depends(require_admin)])
    def test_connection(body: ConnectionTest) -> dict[str, Any]:
        try:
            s = merged_settings(body.model_dump())
        except ValueError as exc:
            raise bad_request(exc) from exc
        user = _user.get()
        if not s.immich_url or not (s.immich_api_key or s.multi_user):
            raise bad_request(ValueError("Enter the Immich URL and an API key."))
        if s.immich_api_key:
            return _probe(make_client(s))
        if user is not None and s.immich_url == runtime.settings.immich_url:
            # With several users and no API key: test with this person's login (never sent to another server).
            return _probe(_use(make_client(s), user.kind, user.secret))
        return _probe(make_client(s), reach_only=True)

    @app.put("/api/settings", dependencies=[Depends(require_admin)])
    def save_settings(body: dict[str, Any]) -> dict[str, Any]:
        try:
            s = merged_settings(body)
        except (ValueError, TypeError) as exc:
            raise bad_request(exc) from exc
        if not s.configured:
            raise bad_request(missing(s))
        moved = s.immich_url != runtime.settings.immich_url
        s.save(exclude=env_overrides())
        runtime.reconfigure(s)
        if s.multi_user:
            runtime.claim_single_user_data()
        # Turning on several users, or another Immich server (its sessions are gone): log in next.
        return {"ok": True, "login": s.multi_user and (_user.get() is None or moved)}

    # --- data -------------------------------------------------------------------------------

    def assets_of(album_id: str, ws: Workspace | None = None) -> list[Asset]:
        """The photos of an album, or of a list from the library overview."""
        ws = ws or workspace()
        if is_list(album_id):
            return ws.library.list_assets(album_id)
        return ws.guesser.immich.album_assets(album_id)

    @app.get("/api/albums/{album_id}/assets")
    def album_assets(album_id: str) -> dict[str, Any]:
        guesser = g()
        try:
            assets = assets_of(album_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
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
                "type": a.type,
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
        album = body.album_id if body.remove_from_album and not is_list(body.album_id) else None
        job = submit("known-date", lambda progress: guesser.apply_known_date(
            body.asset_ids, spec, keep_order=body.keep_order, remove_from_album=album, progress=progress),
            label="Writing dates", meta={"album_id": body.album_id})
        return job.to_dict()

    @app.post("/api/estimate")
    def estimate(body: EstimateRequest) -> dict[str, Any]:
        guesser = g()
        try:
            not_before = parse_year_bound(body.not_before, end=False)
            not_after = parse_year_bound(body.not_after, end=True)
        except ValueError as exc:
            raise bad_request(exc) from exc
        job = submit("estimate", lambda progress: guesser.estimate(
            body.asset_ids, joint=body.joint, not_before=not_before, not_after=not_after, progress=progress),
            label="Analysing faces", meta={"album_id": body.album_id})
        return job.to_dict()

    @app.post("/api/name-dates")
    def name_dates(body: NameDateRequest) -> dict[str, Any]:
        ws = workspace()
        wanted = set(body.asset_ids)

        def run(progress: Callable[[int, int], None]) -> dict[str, Any]:
            assets = [a for a in assets_of(body.album_id, ws) if a.id in wanted]
            return ws.guesser.suggest_name_dates(assets, progress=progress)

        return submit("name-dates", run, label="Reading file names", meta={"album_id": body.album_id}).to_dict()

    @app.post("/api/apply-estimates")
    def apply_estimates(body: ApplyRequest) -> dict[str, Any]:
        """Write the suggestions of the given photos: the date from the file name where there is one,
        else the estimate from faces."""
        guesser = g()
        album = body.album_id if body.remove_from_album and not is_list(body.album_id) else None
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

        return submit("apply", run, label="Writing dates", meta={"album_id": body.album_id}).to_dict()

    @app.post("/api/discard-estimates")
    def discard_estimates(body: AssetIds) -> dict[str, Any]:
        guesser = g()
        guesser.store.delete_estimates(body.asset_ids)
        guesser.store.delete_suggestions("name_date", body.asset_ids)
        return {"discarded": len(body.asset_ids)}

    @app.post("/api/calibrate")
    def calibrate(body: CalibrateRequest) -> dict[str, Any]:
        guesser = g()
        job = submit("calibrate", lambda progress: report_dict(
            guesser.calibrate(per_person=body.per_person, progress=progress)), label="Calibrating the age model")
        return job.to_dict()

    # --- library overview ---------------------------------------------------------------------

    @app.get("/api/library")
    def library_summary() -> dict[str, Any]:
        return library().summary() or {"scanned_at": None}

    @app.post("/api/library/scan")
    def library_scan() -> dict[str, Any]:
        lib = library()
        return submit("library-scan", lambda progress: lib.scan(progress=progress),
                      label="Scanning the library", meta={"href": "/#library"}).to_dict()

    # --- versions of a photo (HDR, panoramas, edits) -------------------------------------------

    @app.post("/api/versions/find")
    def versions_find(body: VersionFindRequest) -> dict[str, Any]:
        v = versions()
        return submit("versions-find", lambda progress: v.find(body.album_id or None, progress=progress),
                      label="Looking for versions", meta={"page": "dates"}).to_dict()

    @app.get("/api/versions")
    def versions_list() -> list[dict[str, Any]]:
        items = [{"id": aid, **data} for aid, data in versions().suggestions().items()]
        return sorted(items, key=lambda x: x["file_name"].lower())

    @app.post("/api/versions/apply")
    def versions_apply(body: AssetIds) -> dict[str, Any]:
        v = versions()
        return submit("versions-apply", lambda progress: v.apply(body.asset_ids, progress=progress),
                      label="Copying dates from originals", meta={"page": "dates"}).to_dict()

    @app.post("/api/versions/discard")
    def versions_discard(body: AssetIds) -> dict[str, Any]:
        versions().discard(body.asset_ids)
        return {"discarded": len(body.asset_ids)}

    # --- locations ----------------------------------------------------------------------------

    @app.get("/api/tracks")
    def list_tracks() -> list[dict[str, Any]]:
        return geo().tracks()

    @app.post("/api/tracks")
    async def upload_track(request: Request, file_name: str = "track.gpx") -> dict[str, Any]:
        tagger = geo()
        data = bytearray()
        async for chunk in request.stream():  # stop reading as soon as the file is too large
            data += chunk
            if len(data) > MAX_GPX_BYTES:
                raise HTTPException(status_code=413, detail="The file is too large (more than 50 MB).")
        try:
            return await run_in_threadpool(tagger.add_track, file_name[:200], bytes(data))
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

    def geo_options(body: GeoRequest) -> GeoOptions:
        try:
            tz_name = body.camera_tz or runtime.settings.timezone
            tz = _zone(tz_name)
            opts = GeoOptions(
                track_ids=body.track_ids, use_tracks=body.use_tracks, start=_local(body.start, tz),
                end=_local(body.end, tz), album_id=body.album_id or None, camera_tz=tz_name,
                use_photo_tz=body.use_photo_tz, offset_s=body.offset_s, tolerance_min=body.tolerance_min,
                max_gap_min=body.max_gap_min, use_nearby=body.use_nearby, nearby_min=body.nearby_min,
                use_siblings=body.use_siblings, include_located=body.include_located,
                camera_offsets={k: v for k, v in body.camera_offsets.items() if abs(v) <= 30 * 86400})
        except ValueError as exc:
            raise bad_request(exc) from exc
        if bool(opts.start) != bool(opts.end):
            raise bad_request(ValueError("Enter both ends of the time range, or neither."))
        return opts

    @app.post("/api/geo/find")
    def geo_find(body: GeoRequest) -> dict[str, Any]:
        tagger, opts = geo(), geo_options(body)
        return submit("geo-find", lambda progress: tagger.find(opts, progress=progress),
                      label="Searching photos without a location", meta={"page": "locations"}).to_dict()

    @app.post("/api/geo/clock-check")
    def geo_clock_check(body: GeoRequest) -> dict[str, Any]:
        """Work out the cameras' clock offsets from photos of the same moments (nothing is written)."""
        tagger, opts = geo(), geo_options(body)
        return submit("geo-clocks", lambda progress: tagger.check_clocks(opts, progress=progress),
                      label="Checking camera clocks", meta={"page": "locations"}).to_dict()

    @app.get("/api/geo/suggestions")
    def geo_suggestions() -> dict[str, Any]:
        tagger = geo()
        items = [{"id": aid, **data} for aid, data in tagger.suggestions().items()]
        items.sort(key=lambda x: x.get("instant") or "")
        for it in items:  # for photos without a time zone: the zone of the suggested place
            if it.get("status") == "ok" and lacks_zone(it.get("time_zone")) and it.get("instant"):
                it["place_tz"] = place_zone(it["lat"], it["lon"])
        return {"run": tagger.run(), "items": items}

    @app.get("/api/geo/clock-candidates")
    def clock_candidates(asset_id: str, camera_tz: str | None = None, use_photo_tz: bool = True,
                         hours: float = 6) -> dict[str, Any]:
        tagger = geo()
        tz_name = camera_tz or tagger.settings.timezone
        try:
            _zone(tz_name)
            return tagger.clock_candidates(asset_id, camera_tz=tz_name, use_photo_tz=use_photo_tz,
                                           hours=min(max(hours, 0.5), 48))
        except ValueError as exc:
            raise bad_request(exc) from exc
        except ImmichError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/geo/place")
    def geo_place(body: PlaceRequest) -> dict[str, Any]:
        try:
            return {"placed": geo().place(body.asset_ids, body.lat, body.lon, body.label)}
        except ValueError as exc:
            raise bad_request(exc) from exc

    @app.get("/api/geocode")
    def geocode(q: str, lang: str | None = None, lat: float | None = None, lon: float | None = None
                ) -> dict[str, Any]:
        near = (lat, lon) if lat is not None and lon is not None else None
        try:
            results = geocode_search(g().settings.geocoder_url, q, lang=lang, near=near,
                                     transport=geocode_transport)
        except GeocodeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"results": results}

    @app.post("/api/place-names")
    def place_names(body: PlaceNamesRequest) -> dict[str, Any]:
        """Names of the places at the given positions, from the cache or (a few per call) the geocoder."""
        guesser = g()
        s = guesser.settings
        if not (s.place_names and s.geocoder_url):
            return {"names": {}, "pending": 0, "enabled": False}
        lang = (body.lang or "")[:2].lower()
        keys = [k for k in dict.fromkeys(body.keys) if _PLACE_KEY.match(k)]
        cache_key = {k: f"{k}|{lang}" for k in keys}
        cached = guesser.store.place_names(list(cache_key.values()))
        names = {k: cached[c] for k, c in cache_key.items() if c in cached}
        missing = [k for k in keys if k not in names]
        for k in missing[:PLACE_BATCH]:
            lat, lon = (float(x) for x in k.split(","))
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                continue
            try:
                name = reverse_geocode(s.geocoder_url, lat, lon, lang=lang, transport=geocode_transport)
            except GeocodeError as exc:
                log.warning("Place name lookup failed: %s", exc)
                return {"names": names, "pending": 0, "enabled": True, "error": str(exc)}
            guesser.store.save_place_name(cache_key[k], name)
            names[k] = name
        return {"names": names, "pending": max(0, len(missing) - PLACE_BATCH), "enabled": True}

    @app.get("/api/geo/no-location")
    def list_no_location() -> list[dict[str, str]]:
        return geo().no_location()

    @app.post("/api/geo/no-location")
    def mark_no_location(body: AssetIds) -> dict[str, Any]:
        return {"marked": geo().mark_no_location(body.asset_ids)}

    @app.post("/api/geo/no-location/remove")
    def unmark_no_location(body: AssetIds) -> dict[str, Any]:
        geo().unmark_no_location(body.asset_ids)
        return {"unmarked": len(body.asset_ids)}

    @app.post("/api/geo/discard")
    def geo_discard(body: AssetIds) -> dict[str, Any]:
        geo().discard(body.asset_ids)
        return {"discarded": len(body.asset_ids)}

    @app.post("/api/geo/apply")
    def geo_apply(body: GeoApplyRequest) -> dict[str, Any]:
        tagger = geo()
        return submit("geo-apply", lambda progress: tagger.apply(
            body.asset_ids, fix_time=body.fix_time, local_time=body.local_time, progress=progress),
            label="Writing locations", meta={"page": "locations"}).to_dict()

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
        # Not cancellable: removing locations needs its second step, waiting for Immich, to finish.
        return submit("undo", lambda progress: guesser.history.undo(batch_id, progress=progress),
                      label="Undoing a change", meta={"page": "history"}, cancellable=False).to_dict()

    @app.get("/api/jobs")
    def active_jobs() -> list[dict[str, Any]]:
        """Jobs waiting or running, so that pages can show them again after a reload."""
        return [j.to_dict() for j in jobs.active(owner())]

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str) -> dict[str, Any]:
        job = jobs.get(job_id, owner())
        if job is None:
            raise HTTPException(status_code=404, detail="Unknown job")
        return job.to_dict()

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str) -> dict[str, Any]:
        try:
            job = jobs.cancel(job_id, owner())
        except ValueError as exc:
            raise bad_request(exc) from exc
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


def _probe(client: ImmichClient, reach_only: bool = False) -> dict[str, Any]:
    """Check that Immich is reachable and the key can read what the tool needs."""
    checks: list[dict[str, Any]] = []
    try:
        try:
            version = client.server_version()
        except ImmichError as exc:
            return {"ok": False, "checks": [{"label": "Reach Immich", "ok": False, "detail": str(exc)}]}
        checks.append({"label": f"Reach Immich {version}", "ok": True, "detail": ""})
        for label, fn in [] if reach_only else [
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
