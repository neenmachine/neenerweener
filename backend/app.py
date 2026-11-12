import asyncio
import json
import logging
import os
import posixpath
import re
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from html import escape, unescape
from pathlib import Path
from urllib.parse import quote

import yaml
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from markdown_it import MarkdownIt
from mdit_py_plugins.dollarmath import dollarmath_plugin
from pydantic import BaseModel, ConfigDict, PositiveInt, ValidationError
from pygments import highlight as pygments_highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import TextLexer, get_lexer_by_name, get_lexer_for_filename
from pygments.util import ClassNotFound

from anchors import add_heading_anchors
from agent_commons import AgentCommonsStore, InvalidProof as AgentInvalidProof, Throttled as AgentThrottled, AGENT_SCOPES, CHALLENGE_SECONDS as AGENT_CHALLENGE_SECONDS, TOKEN_SECONDS as AGENT_TOKEN_SECONDS, WritesPaused
from canvas import CanvasCooldown, CanvasStore, authorize_canvas_write, png_bytes, snapshot_payload
from operations import check_readiness, configure_logging, load_schema_heads
from page_markup import home_markup, site_footer
import post_images
from post_catalog import CatalogUnavailable, PostCatalog, PostRedirect
import posts
import routes

POSTS_DIR = Path(os.environ.get("POSTS_DIR", Path(__file__).parent.parent / "posts"))
DRAFTS_DIR = Path(os.environ.get("DRAFTS_DIR", Path(__file__).parent.parent / "drafts"))
CODE_DIR = Path(os.environ.get("CODE_DIR", Path(__file__).parent.parent / "code"))
FRONTEND_DIST = Path(os.environ.get("FRONTEND_DIST", Path(__file__).parent.parent / "frontend" / "dist"))
EXPECTED_SCHEMA_REVISIONS = load_schema_heads(Path(__file__).with_name("alembic.ini"))
FILENAME = re.compile(r"^([a-z0-9-]+)\.md$")
PROJECT = re.compile(r"[a-z0-9-]+")
from auth import (
    CSRF_HEADER,
    RATE_LIMIT_KEY_ERROR,
    AdminConfig,
    AuthStore,
    Session,
    valid_csrf,
    verify_password,
)
from client_identity import railway_client_ip
from cache import cache_control, etag_response
from machine_auth import (
    MAX_REQUEST_BYTES,
    NAMESPACE,
    TOKEN_SECONDS,
    InvalidProof,
    MachineAuthConfig,
    MachineAuthStore,
    Principal,
    Throttled,
)
from media import (
    CONTENT_TYPE,
    MAX_UPLOAD_BYTES,
    VARIANTS,
    MediaConfig,
    MediaNotFound,
    MediaStore,
    SM_EDGE,
    S3MediaStore,
    UnsupportedImage,
    fit,
    media_key,
    process_image,
    process_avatar,
    store_image,
    sweep,
)
from timeline import (
    TORONTO,
    InvalidAttachment,
    TimelineStore,
    TimelineStoreProtocol,
    serialize_entry,
    validate_timeline_create,
)

logger = logging.getLogger(__name__)


def route_template(request: Request) -> str:
    route = request.scope.get("route")
    template = getattr(route, "path", None)
    if isinstance(template, str) and template.startswith("/") and len(template) <= 256:
        return template
    return "<unknown>"


def highlight(code: str, lang: str, _attrs) -> str:
    if lang == "mermaid":
        return f'<pre class="mermaid">{escape(code)}</pre>'  # the frontend renders these
    try:
        lexer = get_lexer_by_name(lang)
    except ClassNotFound:
        return ""  # markdown-it falls back to a plain escaped block
    return pygments_highlight(code, lexer, HtmlFormatter(nowrap=True))


# CommonMark + GitHub tables and strikethrough. Raw HTML is on: posts are Henry's own trusted files.
md = MarkdownIt("commonmark", {"highlight": highlight}).enable(["table", "strikethrough"])
dollarmath_plugin(md, allow_digits=False)
md.core.ruler.after("inline", "heading_anchors", add_heading_anchors)

def _load_machine_auth_state() -> tuple[bool, MachineAuthConfig]:
    return os.environ.get("ADMIN_SSH_KEYS") is not None, MachineAuthConfig.from_env()


@asynccontextmanager
async def lifespan(_app):
    configured, config = await asyncio.to_thread(_load_machine_auth_state)
    _app.state.machine_auth_configured = configured
    _app.state.machine_auth_config = config
    if _admin_config().rate_limit_key is None:
        logger.warning(RATE_LIMIT_KEY_ERROR)
    global media_store
    media_config = await asyncio.to_thread(MediaConfig.from_env)
    _app.state.media_config = media_config
    if media_config.partial:
        logger.warning("media configuration incomplete; missing %s", ", ".join(media_config.missing))
    elif media_config.complete and media_store is None:
        media_store = await asyncio.to_thread(S3MediaStore, media_config)
    yield
    if timeline_store is not None:
        await asyncio.to_thread(timeline_store.close)
    if auth_store is not None:
        await asyncio.to_thread(auth_store.close)
    if machine_auth_store is not None:
        await asyncio.to_thread(machine_auth_store.close)
    if agent_commons_store is not None:
        await asyncio.to_thread(agent_commons_store.close)
    if canvas_store is not None:
        await asyncio.to_thread(canvas_store.close)
    if post_store is not None:
        await asyncio.to_thread(post_store.close)


app = FastAPI(lifespan=lifespan)
app.state.machine_auth_configured = False
app.state.machine_auth_config = MachineAuthConfig({}, None, None, False)
app.state.media_config = MediaConfig()
configure_logging()
timeline_store: TimelineStoreProtocol | None = (
    TimelineStore(os.environ["DATABASE_URL"]) if os.environ.get("DATABASE_URL") else None
)
post_store: posts.PostStore | None = (
    posts.PostStore(os.environ["DATABASE_URL"], reserved_slugs=lambda: _published_file_slugs())
    if os.environ.get("DATABASE_URL")
    else None
)
auth_store: AuthStore | None = AuthStore(os.environ["DATABASE_URL"]) if os.environ.get("DATABASE_URL") else None
agent_commons_store: AgentCommonsStore | None = (
    AgentCommonsStore(os.environ["DATABASE_URL"]) if os.environ.get("DATABASE_URL") else None
)
canvas_store: CanvasStore | None = (
    CanvasStore(os.environ["DATABASE_URL"]) if os.environ.get("DATABASE_URL") else None
)
machine_auth_store: MachineAuthStore | None = (
    MachineAuthStore(os.environ["DATABASE_URL"]) if os.environ.get("DATABASE_URL") else None
)
post_catalog = PostCatalog(lambda: all_posts(), post_store)
media_store: MediaStore | None = None  # set at startup when all MEDIA_* variables are present


def require_post_catalog_ready() -> None:
    try:
        conflicts = post_catalog.conflicts()
    except CatalogUnavailable as exc:
        raise RuntimeError("post catalog preflight failed: database unavailable") from exc
    if conflicts:
        raise RuntimeError(f"post catalog preflight failed: conflicting slug {conflicts[0]}")


_decode = asyncio.Semaphore(1)  # one decode per process, so two large uploads cannot double peak RAM
MEDIA_SWEEP_INTERVAL_SECONDS = 600
_media_sweep_due = 0.0  # time.monotonic() after which the next upload may schedule a sweep


@app.exception_handler(RequestValidationError)
async def admin_validation_error(request: Request, exc: RequestValidationError):
    if request.url.path.startswith(("/api/admin/", "/api/cli/auth/")):
        return JSONResponse({"detail": "invalid request"}, status_code=422, headers={"Cache-Control": "no-store"})
    return await request_validation_exception_handler(request, exc)


@app.middleware("http")
async def operational_middleware(request: Request, call_next):
    started = time.perf_counter()
    request_id = uuid.uuid4().hex
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-ID"] = request_id
        return response
    except Exception:
        logger.exception("request failed", extra={"request_id": request_id})
        response = JSONResponse({"detail": "internal server error"}, status_code=500)
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        logger.info(
            "request",
            extra={
                "request_id": request_id,
                "method": request.method,
                "route": route_template(request),
                "status": status,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    response = await call_next(request)
    policy = cache_control(request.url.path, response.status_code, response.headers)
    if policy is not None:
        response.headers["Cache-Control"] = policy
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), geolocation=(), microphone=(), payment=(), usb=()"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Content-Security-Policy-Report-Only"] = (
        # blob: for the local previews of images being attached in the admin compose form
        "default-src 'self'; script-src 'self'; img-src 'self' blob:; connect-src 'self'; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    )
    return response


@app.api_route("/healthz", methods=["GET", "HEAD"])
async def healthz():
    return {"status": "ok"}


@app.api_route("/api/profile", methods=["GET", "HEAD"])
def get_profile():
    if timeline_store is None:
        raise HTTPException(503, "profile unavailable")
    try:
        return _public_profile(timeline_store.get_profile())
    except Exception as exc:
        logger.warning("profile unavailable: %s", type(exc).__name__)
        raise HTTPException(503, "profile unavailable") from exc


@app.api_route("/api/timeline", methods=["GET", "HEAD"])
def list_timeline(
    before: int | None = Query(default=None, gt=0),
    limit: int = Query(default=20, ge=1, le=20),
):
    if timeline_store is None:
        raise HTTPException(503, "timeline unavailable")
    try:
        rows = timeline_store.list(before, limit)
    except Exception as exc:
        logger.warning("timeline unavailable: %s", type(exc).__name__)
        raise HTTPException(503, "timeline unavailable") from exc
    has_next = len(rows) > limit
    entries = []
    for row in rows[:limit]:
        try:
            entries.append(serialize_entry(row))
        except Exception as exc:
            logger.warning("invalid timeline entry id=%s error=%s", row.get("id"), type(exc).__name__)
    return {
        "entries": entries,
        "next_cursor": rows[limit - 1]["id"] if has_next and rows else None,
        # The one clock for every relative time on this page; older pages and new entries keep the first page's.
        "now": utc_now().isoformat(),
    }


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str


class Attachment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: uuid.UUID
    alt: str = ""


class TimelineMutation(BaseModel):
    """An edit: attachments is the full ordered image list; omitted (None) leaves the images as they are."""

    model_config = ConfigDict(extra="forbid")
    body_markdown: str
    attachments: list[Attachment] | None = None


class TimelineCreate(TimelineMutation):
    attachments: list[Attachment] = []


class ProfileMutation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str
    handle: str | None
    avatar_id: uuid.UUID | None = None


class PostCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str
    slug: str
    body_markdown: str = ""
    excerpt: str | None = None
    tags: list[str] = []
    published_on: str | None = None


class PostMutation(PostCreate):
    version: PositiveInt
    status: str


class PostVersion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: PositiveInt


class PostPreview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    body_markdown: str
    # Optional: first move (to before top-level block move_to) or resize (to width) one managed image of the body.
    media_id: str | None = None
    move_to: int | None = None
    width: int | None = None


MAX_TIMELINE_JSON_BYTES = 65_536  # body plus four alt texts; matches the CLI's JSON cap
MAX_POST_JSON_BYTES = 2 * 1024 * 1024


class ChallengeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key_fingerprint: str


class TokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    challenge_id: uuid.UUID
    signature: str


class CanvasPixelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    x: int
    y: int
    color: int


class CanvasClearRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    x: int
    y: int
    width: int
    height: int


# What /timeline shows when the profile cannot be read: the migration's defaults, so the feed still renders.
DEFAULT_PROFILE = {"display_name": "neenerweener", "handle": None, "avatar_asset_id": None}


def _public_profile(profile: dict) -> dict:
    display_name = profile["display_name"]
    return {
        "display_name": display_name,
        "handle": profile["handle"],
        "avatar_id": str(profile["avatar_asset_id"]) if profile["avatar_asset_id"] else None,
        "avatar_initial": display_name.strip()[0].upper(),
    }


def trusted_client_ip(request: Request) -> str | None:
    return railway_client_ip(request)


def _admin_config() -> AdminConfig:
    return AdminConfig.from_env()


def _check_origin(request: Request, config: AdminConfig) -> None:
    if not config.origin_valid or request.headers.get("Origin") != config.origin:
        raise HTTPException(403, "origin not allowed")


def _clear_session_cookie(response: Response, config: AdminConfig) -> None:
    response.delete_cookie(
        config.cookie_name,
        path="/",
        secure=config.secure_cookie,
        httponly=True,
        samesite="strict",
    )


# A non-secret hint that tells /timeline whether to check the session and picks the /admin title; never used for authorization.
ADMIN_HINT_COOKIE = "admin_hint"


def _clear_hint_cookie(response: Response, config: AdminConfig) -> None:
    response.delete_cookie(ADMIN_HINT_COOKIE, path="/", secure=config.secure_cookie, samesite="strict")


def _machine_config(request: Request) -> MachineAuthConfig:
    return request.app.state.machine_auth_config


def _load_session(request: Request) -> tuple[str, Session, AdminConfig]:
    config = _admin_config()
    if not config.valid:
        raise HTTPException(401, "authentication required")
    token = request.cookies.get(config.cookie_name)
    if not token:
        raise HTTPException(401, "authentication required")
    if auth_store is None:
        raise HTTPException(503, "admin unavailable")
    try:
        session = auth_store.session(token, config.password_hash or "")
    except Exception as exc:
        logger.warning("admin storage unavailable: %s", type(exc).__name__)
        raise HTTPException(503, "admin unavailable") from exc
    if session is None:
        raise HTTPException(401, "authentication required")
    return token, session, config


@dataclass(frozen=True)
class Access:
    principal: Principal
    session: tuple[str, Session, AdminConfig] | None = None


def _load_bearer(request: Request, required_scope: str) -> Access:
    authorization = request.headers.get("Authorization")
    if not authorization or not authorization.startswith("Bearer ") or authorization.count(" ") != 1:
        raise HTTPException(401, "authentication required")
    token = authorization[7:]
    if not token or machine_auth_store is None:
        raise HTTPException(401, "authentication required")
    try:
        principal = machine_auth_store.authenticate(token, _machine_config(request))
    except Exception as exc:
        logger.warning("machine_auth.bearer.storage_unavailable")
        raise HTTPException(503, "admin unavailable") from exc
    if principal is None:
        raise HTTPException(401, "authentication required")
    if not principal.allows(required_scope):
        raise HTTPException(403, "insufficient scope")
    return Access(principal)


def require_access(required_scope: str, *, mutation: bool, bearer: bool = False):
    def authorize(request: Request) -> Access:
        if request.headers.get("Authorization") is not None:
            if not bearer:
                raise HTTPException(401, "authentication required")
            return _load_bearer(request, required_scope)
        token, session, config = _load_session(request)
        if mutation:
            _check_origin(request, config)
            if not valid_csrf(session, request.headers.get(CSRF_HEADER)):
                raise HTTPException(403, "csrf validation failed")
        return Access(Principal("browser-admin", None), (token, session, config))

    return authorize


require_browser_mutation = require_access("browser:admin", mutation=True)
require_timeline_create = require_access("timeline:create", mutation=True, bearer=True)
require_browser_read = require_access("browser:admin", mutation=False)


def _admin_entry(entry: dict) -> dict:
    try:
        return serialize_entry(entry, admin=True)
    except ValueError as exc:
        raise HTTPException(422, "invalid timeline entry") from exc


def _timeline_storage() -> TimelineStoreProtocol:
    if timeline_store is None:
        raise HTTPException(503, "admin unavailable")
    return timeline_store


def _permit_machine(request: Request, access: Access, event: str, limit: int) -> None:
    if access.principal.scopes is None:
        return
    assert machine_auth_store is not None
    retry = machine_auth_store.permit(event, limit, access.principal.subject, _machine_config(request))
    if retry is not None:
        raise HTTPException(429, "too many requests", headers={"Retry-After": str(retry)})


async def _bounded_body(request: Request, limit: int) -> bytes:
    """Read the body, rejecting a declared or streamed size above `limit`."""
    length = request.headers.get("Content-Length")
    if length is not None:
        try:
            parsed_length = int(length)
            if parsed_length < 0:
                raise ValueError
            if parsed_length > limit:
                raise HTTPException(413, "request too large")
        except ValueError as exc:
            raise HTTPException(400, "invalid request") from exc
    chunks = bytearray()
    async for chunk in request.stream():
        chunks.extend(chunk)
        if len(chunks) > limit:
            raise HTTPException(413, "request too large")
    return bytes(chunks)


async def _bounded_json(
    request: Request, model, *, invalid_status: int = 422, invalid_detail: str = "invalid request", limit: int = MAX_REQUEST_BYTES
):
    if request.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(415, "application/json required")
    body = await _bounded_body(request, limit)
    try:
        return model.model_validate_json(body)
    except ValidationError as exc:
        raise HTTPException(invalid_status, invalid_detail) from exc


def _machine_store() -> MachineAuthStore:
    if machine_auth_store is None:
        raise HTTPException(503, "machine authentication unavailable")
    return machine_auth_store


class AgentRegisterRequest(BaseModel):
    handle: str
    public_key: str

class AgentChallengeRequest(BaseModel):
    key_fingerprint: str

class AgentProofRequest(BaseModel):
    challenge_id: uuid.UUID
    signature: str


def _agent_store() -> AgentCommonsStore:
    if agent_commons_store is None:
        raise HTTPException(503, "agent authentication unavailable")
    return agent_commons_store


def _agent_json(request: Request, model, **kwargs):
    return _bounded_json(request, model, **kwargs)


def _agent_challenge_response(challenge):
    return {"challenge_id": str(challenge.challenge_id), "message": challenge.message, "namespace": "neenerweener-agent-v1", "expires_at": challenge.expires_at.isoformat(timespec="seconds").replace("+00:00", "Z")}


@app.post("/api/agents/auth/register/challenge", status_code=201)
async def agent_register_challenge(request: Request):
    payload = await _agent_json(request, AgentRegisterRequest)
    try:
        challenge = await asyncio.to_thread(_agent_store().registration_challenge, payload.handle, payload.public_key, trusted_client_ip(request))
    except AgentThrottled as exc:
        raise HTTPException(429, "too many requests", headers={"Retry-After": str(exc.retry_after)}) from exc
    except (ValueError, TypeError):
        raise HTTPException(422, "invalid request")
    except Exception as exc:
        logger.warning("agent_auth.registration_challenge.failed")
        raise HTTPException(503, "agent authentication unavailable") from exc
    return _agent_challenge_response(challenge)


@app.post("/api/agents/auth/register", status_code=201)
async def agent_register(request: Request):
    payload = await _agent_json(request, AgentProofRequest, invalid_status=401, invalid_detail="invalid key proof")
    try:
        principal = await asyncio.to_thread(_agent_store().register, payload.challenge_id, payload.signature, trusted_client_ip(request))
    except AgentInvalidProof as exc:
        raise HTTPException(401, "invalid key proof") from exc
    except Exception as exc:
        if isinstance(exc, ValueError): raise HTTPException(409, "handle or key already registered") from exc
        logger.warning("agent_auth.registration.failed")
        raise HTTPException(503, "agent authentication unavailable") from exc
    return {"agent_id": str(principal.agent_id), "handle": principal.handle, "fingerprint": principal.fingerprint, "scopes": sorted(principal.scopes)}


@app.post("/api/agents/auth/challenge", status_code=201)
async def agent_challenge(request: Request):
    payload = await _agent_json(request, AgentChallengeRequest)
    try:
        challenge = await asyncio.to_thread(_agent_store().challenge, payload.key_fingerprint, trusted_client_ip(request))
    except AgentThrottled as exc:
        raise HTTPException(429, "too many requests", headers={"Retry-After": str(exc.retry_after)}) from exc
    except AgentInvalidProof as exc:
        raise HTTPException(401, "invalid key proof") from exc
    except Exception as exc:
        logger.warning("agent_auth.challenge.failed")
        raise HTTPException(503, "agent authentication unavailable") from exc
    return _agent_challenge_response(challenge)


@app.post("/api/agents/auth/token")
async def agent_token(request: Request):
    payload = await _agent_json(request, AgentProofRequest, invalid_status=401, invalid_detail="invalid key proof")
    try:
        token, principal = await asyncio.to_thread(_agent_store().token, payload.challenge_id, payload.signature, trusted_client_ip(request))
    except AgentThrottled as exc:
        raise HTTPException(429, "too many requests", headers={"Retry-After": str(exc.retry_after)}) from exc
    except AgentInvalidProof as exc:
        raise HTTPException(401, "invalid key proof") from exc
    except Exception as exc:
        logger.warning("agent_auth.token.failed")
        raise HTTPException(503, "agent authentication unavailable") from exc
    return {"access_token": token, "token_type": "Bearer", "expires_in": AGENT_TOKEN_SECONDS, "scopes": sorted(principal.scopes)}


@app.get("/api/agents/me")
async def agent_me(request: Request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth.count(" ") != 1:
        raise HTTPException(401, "authentication required")
    principal = await asyncio.to_thread(_agent_store().authorize, auth[7:])
    if principal is None: raise HTTPException(401, "authentication required")
    return {"agent_id": str(principal.agent_id), "handle": principal.handle, "fingerprint": principal.fingerprint, "scopes": sorted(principal.scopes), "banned": False}


def _canvas_store() -> CanvasStore:
    if canvas_store is None:
        raise HTTPException(503, "canvas unavailable")
    return canvas_store


def _canvas_token(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth.count(" ") != 1 or not auth[7:]:
        raise HTTPException(401, "authentication required")
    return auth[7:]


@app.get("/api/canvas")
async def canvas_snapshot(request: Request):
    revision, pixels = await asyncio.to_thread(_canvas_store().snapshot)
    content = json.dumps(snapshot_payload(revision, pixels), separators=(",", ":")).encode()
    return etag_response(request, content, "application/json", {"Cache-Control": "public, max-age=3, must-revalidate"})


@app.get("/api/canvas/events")
async def canvas_events(request: Request, after: int = Query(0, ge=0)):
    value = await asyncio.to_thread(_canvas_store().events, after)
    return etag_response(request, json.dumps(jsonable_encoder(value), separators=(",", ":")).encode(), "application/json", {"Cache-Control": "public, max-age=3, must-revalidate"})


@app.get("/api/canvas/history")
async def canvas_history(request: Request, after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
    value = await asyncio.to_thread(_canvas_store().events, after, limit)
    return etag_response(request, json.dumps(jsonable_encoder(value), separators=(",", ":")).encode(), "application/json", {"Cache-Control": "public, max-age=3, must-revalidate"})


@app.post("/api/canvas/pixels", status_code=201)
async def canvas_pixel(request: Request):
    payload = await _bounded_json(request, CanvasPixelRequest, limit=1024)
    if not (0 <= payload.x < 64 and 0 <= payload.y < 64 and 0 <= payload.color < 16):
        raise HTTPException(422, "invalid pixel")
    try:
        principal = await asyncio.to_thread(authorize_canvas_write, _agent_store(), _canvas_token(request))
        result = await asyncio.to_thread(_canvas_store().place, principal, payload.x, payload.y, payload.color)
    except AgentThrottled as exc:
        raise HTTPException(429, "too many requests", headers={"Retry-After": str(exc.retry_after)}) from exc
    except PermissionError as exc:
        raise HTTPException(403, "canvas write not permitted") from exc
    except WritesPaused as exc:
        raise HTTPException(503, "agent writes paused") from exc
    except CanvasCooldown as exc:
        raise HTTPException(429, "canvas cooldown active", headers={"Retry-After": str(exc.retry_after)}) from exc
    return JSONResponse(result, status_code=201, headers={"Cache-Control": "no-store"})


@app.get("/api/canvas.png")
async def canvas_png(request: Request):
    revision, pixels = await asyncio.to_thread(_canvas_store().snapshot)
    data = await asyncio.to_thread(png_bytes, pixels)
    return etag_response(request, data, "image/png", {"Cache-Control": "public, max-age=3, must-revalidate", "X-Canvas-Revision": str(revision)}, etag=f'"canvas-revision-{revision}"')


@app.post("/api/admin/agents/canvas/clear")
async def canvas_clear(request: Request, _access: Access = Depends(require_browser_mutation)):
    payload = await _bounded_json(request, CanvasClearRequest, limit=1024)
    if payload.width < 1 or payload.height < 1 or payload.x < 0 or payload.y < 0 or payload.x + payload.width > 64 or payload.y + payload.height > 64:
        raise HTTPException(422, "invalid canvas region")
    revision = await asyncio.to_thread(_canvas_store().clear, payload.x, payload.y, payload.width, payload.height)
    return JSONResponse({"revision": revision}, headers={"Cache-Control": "no-store"})


@app.post("/api/cli/auth/challenge", status_code=201)
async def machine_challenge(request: Request):
    payload = await _bounded_json(request, ChallengeRequest)
    if not payload.key_fingerprint or len(payload.key_fingerprint) > 128:
        raise HTTPException(422, "invalid request")
    try:
        challenge = await asyncio.to_thread(
            _machine_store().issue_challenge,
            payload.key_fingerprint,
            trusted_client_ip(request),
            _machine_config(request),
        )
    except Throttled as exc:
        logger.info("machine_auth.challenge.throttled")
        raise HTTPException(429, "too many requests", headers={"Retry-After": str(exc.retry_after)}) from exc
    except RuntimeError as exc:
        logger.warning("machine_auth.challenge.unavailable")
        raise HTTPException(503, "machine authentication unavailable") from exc
    except Exception as exc:
        logger.warning("machine_auth.challenge.storage_unavailable")
        raise HTTPException(503, "machine authentication unavailable") from exc
    logger.info("machine_auth.challenge.issued")
    return {
        "challenge_id": str(challenge.challenge_id),
        "message": challenge.message,
        "namespace": NAMESPACE,
        "expires_at": challenge.expires_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
    }


async def _reject_machine_proof(request: Request) -> None:
    try:
        retry = await asyncio.to_thread(
            _machine_store().reject_exchange,
            trusted_client_ip(request),
            _machine_config(request),
        )
    except RuntimeError as exc:
        raise HTTPException(503, "machine authentication unavailable") from exc
    except Exception as exc:
        logger.warning("machine_auth.exchange.storage_unavailable")
        raise HTTPException(503, "machine authentication unavailable") from exc
    if retry is not None:
        raise HTTPException(429, "too many requests", headers={"Retry-After": str(retry)})
    raise HTTPException(401, "invalid key proof")


@app.post("/api/cli/auth/token")
async def machine_token(request: Request):
    try:
        payload = await _bounded_json(request, TokenRequest, invalid_status=401, invalid_detail="invalid key proof")
    except HTTPException as exc:
        if exc.status_code == 401:
            await _reject_machine_proof(request)
        raise
    if not payload.signature or len(payload.signature) > 12_000:
        await _reject_machine_proof(request)
    try:
        token = await asyncio.to_thread(
            _machine_store().exchange,
            payload.challenge_id,
            payload.signature,
            trusted_client_ip(request),
            _machine_config(request),
        )
    except Throttled as exc:
        logger.info("machine_auth.exchange.throttled")
        raise HTTPException(429, "too many requests", headers={"Retry-After": str(exc.retry_after)}) from exc
    except InvalidProof as exc:
        logger.info("machine_auth.exchange.rejected")
        raise HTTPException(401, "invalid key proof") from exc
    except RuntimeError as exc:
        logger.warning("machine_auth.exchange.unavailable")
        raise HTTPException(503, "machine authentication unavailable") from exc
    except Exception as exc:
        logger.warning("machine_auth.exchange.storage_unavailable")
        raise HTTPException(503, "machine authentication unavailable") from exc
    logger.info("machine_auth.exchange.succeeded")
    return {
        "access_token": token.access_token,
        "token_type": "Bearer",
        "expires_in": TOKEN_SECONDS,
        "scopes": list(token.scopes),
    }


@app.get("/api/admin/session")
def admin_session(request: Request, response: Response):
    config = _admin_config()
    token = request.cookies.get(config.cookie_name)
    if not config.valid or not token:
        _clear_hint_cookie(response, config)
        return {"authenticated": False}
    if auth_store is None:
        raise HTTPException(503, "admin unavailable")
    try:
        session = auth_store.session(token, config.password_hash or "")
    except Exception as exc:
        logger.warning("admin storage unavailable: %s", type(exc).__name__)
        raise HTTPException(503, "admin unavailable") from exc
    if session is None:
        _clear_session_cookie(response, config)
        _clear_hint_cookie(response, config)
        return {"authenticated": False}
    return {"authenticated": True, "csrf_token": session.csrf_token}


@app.post("/api/admin/login", status_code=204)
def admin_login(payload: LoginRequest, request: Request, response: Response):
    config = _admin_config()
    if not config.origin_valid:
        raise HTTPException(401, "invalid credentials")
    _check_origin(request, config)
    if not config.valid:
        raise HTTPException(401, "invalid credentials")
    if auth_store is None:
        raise HTTPException(503, "admin unavailable")
    identity = config.rate_identity(request.scope.get("headers", []))
    if identity is None:
        raise HTTPException(401, "invalid credentials")
    try:
        retry = auth_store.retry_after(identity)
        if retry is not None:
            raise HTTPException(429, "too many login attempts", headers={"Retry-After": str(retry)})
        try:
            verified = verify_password(payload.password, config.password_hash)
        except UnicodeEncodeError:
            verified = False
        if not verified:
            retry = auth_store.record_failure(identity)
            if retry is not None:
                raise HTTPException(429, "too many login attempts", headers={"Retry-After": str(retry)})
            raise HTTPException(401, "invalid credentials")
        token, session = auth_store.create_session(config.password_hash or "", identity)
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("admin storage unavailable: %s", type(exc).__name__)
        raise HTTPException(503, "admin unavailable") from exc
    response.set_cookie(
        config.cookie_name,
        token,
        max_age=12 * 60 * 60,
        expires=session.expires_at,
        path="/",
        secure=config.secure_cookie,
        httponly=True,
        samesite="strict",
    )
    response.set_cookie(
        ADMIN_HINT_COOKIE,
        "1",
        max_age=12 * 60 * 60,
        expires=session.expires_at,
        path="/",
        secure=config.secure_cookie,
        samesite="strict",
    )


@app.post("/api/admin/logout", status_code=204)
def admin_logout(
    response: Response,
    access: Access = Depends(require_browser_mutation),
):
    assert access.session is not None
    token, _, config = access.session
    try:
        assert auth_store is not None
        auth_store.delete_session(token)
    except Exception as exc:
        logger.warning("admin storage unavailable: %s", type(exc).__name__)
        raise HTTPException(503, "admin unavailable") from exc
    _clear_session_cookie(response, config)
    _clear_hint_cookie(response, config)


async def _timeline_create_body(request: Request) -> TimelineCreate:
    return await _bounded_json(request, TimelineCreate, limit=MAX_TIMELINE_JSON_BYTES)


@app.post("/api/admin/timeline", status_code=201)
def create_admin_timeline(
    request: Request,
    access: Access = Depends(require_timeline_create),
    payload: TimelineCreate = Depends(_timeline_create_body),  # after access: no body is read before auth
):
    attachments = [(attachment.id, attachment.alt) for attachment in payload.attachments]
    try:
        validate_timeline_create(payload.body_markdown, [alt for _id, alt in attachments])
    except InvalidAttachment as exc:
        raise HTTPException(422, "invalid attachment") from exc
    except ValueError as exc:
        raise HTTPException(422, "invalid timeline entry") from exc
    _permit_machine(request, access, "timeline_create", 30)
    try:
        return _admin_entry(_timeline_storage().create(payload.body_markdown, attachments))
    except InvalidAttachment as exc:
        raise HTTPException(422, "invalid attachment") from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("timeline storage unavailable: %s", type(exc).__name__)
        raise HTTPException(503, "admin unavailable") from exc


@app.post("/api/admin/media", status_code=201)
async def upload_media(
    request: Request, background: BackgroundTasks, access: Access = Depends(require_timeline_create)
):
    # The client filename and Content-Type are never read: the format comes from the decoded bytes.
    store, assets = media_store, timeline_store
    if store is None or assets is None:
        raise HTTPException(503, "media unavailable")
    if request.headers.get("Content-Length") is None:
        raise HTTPException(411, "length required")
    await asyncio.to_thread(_permit_machine, request, access, "media_upload", 40)
    data = await _bounded_body(request, MAX_UPLOAD_BYTES)
    try:
        async with _decode:  # waiters park on the event loop, not in an executor thread
            image = await asyncio.to_thread(process_image, data)
    except UnsupportedImage as exc:
        raise HTTPException(422, "unsupported image") from exc
    try:
        asset_id = await asyncio.to_thread(store_image, assets, store, image)
    except Exception as exc:
        logger.warning("media.store.unavailable: %s", type(exc).__name__)
        raise HTTPException(503, "media unavailable") from exc
    _schedule_media_sweep(background, assets, store)
    return {"id": str(asset_id), "width": image.width, "height": image.height}


def _schedule_media_sweep(background: BackgroundTasks, assets, store) -> None:
    global _media_sweep_due
    if time.monotonic() >= _media_sweep_due:
        _media_sweep_due = time.monotonic() + MEDIA_SWEEP_INTERVAL_SECONDS
        background.add_task(_sweep_media, assets, store)  # after the response, so a slow bucket cannot hold it


async def _sweep_media(assets, store) -> None:
    try:
        await asyncio.to_thread(sweep, assets, store)
    except Exception as exc:
        logger.warning("media.sweep.unavailable: %s", type(exc).__name__)


@app.post("/api/admin/profile/avatar", status_code=201)
async def upload_profile_avatar(
    request: Request, background: BackgroundTasks, _access: Access = Depends(require_browser_mutation)
):
    store, assets = media_store, timeline_store
    if store is None or assets is None:
        raise HTTPException(503, "media unavailable")
    if request.headers.get("Content-Length") is None:
        raise HTTPException(411, "length required")
    data = await _bounded_body(request, MAX_UPLOAD_BYTES)
    try:
        async with _decode:
            image = await asyncio.to_thread(process_avatar, data)
    except UnsupportedImage as exc:
        raise HTTPException(422, "unsupported image") from exc
    try:
        asset_id = await asyncio.to_thread(store_image, assets, store, image)
    except Exception as exc:
        logger.warning("media.store.unavailable: %s", type(exc).__name__)
        raise HTTPException(503, "media unavailable") from exc
    _schedule_media_sweep(background, assets, store)
    return {"id": str(asset_id)}


@app.patch("/api/admin/profile")
def update_profile(
    payload: ProfileMutation, _access: Access = Depends(require_browser_mutation)
):
    try:
        avatar = payload.avatar_id if "avatar_id" in payload.model_fields_set else ...
        return _public_profile(_timeline_storage().update_profile(payload.display_name, payload.handle, avatar))
    except InvalidAttachment as exc:
        raise HTTPException(422, "invalid avatar") from exc
    except ValueError as exc:
        raise HTTPException(422, "invalid profile") from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("profile storage unavailable: %s", type(exc).__name__)
        raise HTTPException(503, "admin unavailable") from exc


@app.get("/api/admin/overview")
def admin_overview(_access: Access = Depends(require_browser_read)):
    try:
        return {"entry_count": _timeline_storage().count()}
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("timeline storage unavailable: %s", type(exc).__name__)
        raise HTTPException(503, "admin unavailable") from exc


@app.get("/api/admin/timeline/{entry_id}")
def get_admin_timeline(
    entry_id: int,
    _access: Access = Depends(require_browser_read),
):
    if entry_id <= 0:
        raise HTTPException(422, "invalid timeline id")
    try:
        entry = _timeline_storage().get(entry_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("timeline storage unavailable: %s", type(exc).__name__)
        raise HTTPException(503, "admin unavailable") from exc
    if entry is None:
        raise HTTPException(404, "timeline entry not found")
    return _admin_entry(entry)


@app.patch("/api/admin/timeline/{entry_id}")
def update_admin_timeline(
    entry_id: int,
    payload: TimelineMutation,
    _access: Access = Depends(require_browser_mutation),
):
    if entry_id <= 0:
        raise HTTPException(422, "invalid timeline id")
    attachments = None if payload.attachments is None else [(a.id, a.alt) for a in payload.attachments]
    try:
        validate_timeline_create(payload.body_markdown, [alt for _id, alt in attachments or ()])
    except InvalidAttachment as exc:
        raise HTTPException(422, "invalid attachment") from exc
    except ValueError as exc:
        raise HTTPException(422, "invalid timeline entry") from exc
    try:
        entry = _timeline_storage().update(entry_id, payload.body_markdown, attachments)
    except InvalidAttachment as exc:
        raise HTTPException(422, "invalid attachment") from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("timeline storage unavailable: %s", type(exc).__name__)
        raise HTTPException(503, "admin unavailable") from exc
    if entry is None:
        raise HTTPException(404, "timeline entry not found")
    return _admin_entry(entry)


@app.delete("/api/admin/timeline/{entry_id}", status_code=204)
def delete_admin_timeline(
    entry_id: int,
    _access: Access = Depends(require_browser_mutation),
):
    if entry_id <= 0:
        raise HTTPException(422, "invalid timeline id")
    try:
        deleted = _timeline_storage().delete(entry_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("timeline storage unavailable: %s", type(exc).__name__)
        raise HTTPException(503, "admin unavailable") from exc
    if not deleted:
        raise HTTPException(404, "timeline entry not found")


def _post_storage() -> posts.PostStore:
    if post_store is None:
        raise HTTPException(503, "admin unavailable")
    return post_store


POST_VALIDATION_DETAILS = {
    "invalid title",
    "title too long",
    "invalid body",
    "body too long",
    "invalid excerpt",
    "excerpt too long",
    "invalid tags",
    "invalid date",
    "invalid slug",
    "slug reserved by file post",
    "invalid status",
    "live post needs title and body",
    "slug already used",
    "invalid edit",
    "unknown image",
    "invalid position",
    "invalid width",
}


def _post_operation(operation):
    try:
        return operation()
    except posts.InvalidPost as exc:
        detail = str(exc)
        if detail == "invalid tag":
            detail = "invalid tags"
        if detail not in POST_VALIDATION_DETAILS:
            detail = "invalid post"
        raise HTTPException(422, detail) from exc
    except posts.InvalidAttachment as exc:
        raise HTTPException(422, "image unavailable") from exc
    except ValueError as exc:
        raise HTTPException(422, "invalid post") from exc
    except posts.PostConflict as exc:
        return JSONResponse(
            {"detail": "post version conflict", "current_version": exc.current_version},
            status_code=409,
        )
    except posts.PostNotFound as exc:
        raise HTTPException(404, "post not found") from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("post storage unavailable: %s", type(exc).__name__)
        raise HTTPException(503, "admin unavailable") from exc


async def _post_create_body(request: Request) -> PostCreate:
    return await _bounded_json(request, PostCreate, limit=MAX_POST_JSON_BYTES)


async def _post_mutation_body(request: Request) -> PostMutation:
    return await _bounded_json(request, PostMutation, limit=MAX_POST_JSON_BYTES)


async def _post_version_body(request: Request) -> PostVersion:
    return await _bounded_json(request, PostVersion, limit=MAX_POST_JSON_BYTES)


async def _post_preview_body(request: Request) -> PostPreview:
    return await _bounded_json(request, PostPreview, limit=MAX_POST_JSON_BYTES)


@app.get("/api/admin/posts")
def list_admin_posts(_access: Access = Depends(require_browser_read)):
    return _post_operation(_post_storage().list)


@app.post("/api/admin/posts", status_code=201)
def create_admin_post(
    _access: Access = Depends(require_browser_mutation),
    payload: PostCreate = Depends(_post_create_body),
):
    return _post_operation(lambda: _post_storage().create(**payload.model_dump()))


@app.post("/api/admin/posts/preview")
def preview_admin_post(
    _access: Access = Depends(require_browser_mutation),
    payload: PostPreview = Depends(_post_preview_body),
):
    def preview():
        body = payload.body_markdown
        posts.check_body(body)
        edits = (payload.move_to is not None) + (payload.width is not None)
        if (payload.media_id is None) != (edits == 0) or edits > 1:
            raise posts.InvalidPost("invalid edit")
        if payload.move_to is not None:
            body = post_images.move(md, body, payload.media_id, payload.move_to)
        elif payload.width is not None:
            body = post_images.resize(md, body, payload.media_id, payload.width)
        posts.check_body(body)
        result = post_images.preview(md, body, caption_figures)
        return result if payload.media_id is None else {**result, "body_markdown": body}

    return _post_operation(preview)


@app.get("/api/admin/posts/{post_id}")
def get_admin_post(post_id: PositiveInt, _access: Access = Depends(require_browser_read)):
    document = _post_operation(lambda: _post_storage().get(post_id))
    if document is None:
        raise HTTPException(404, "post not found")
    return document


@app.patch("/api/admin/posts/{post_id}")
def update_admin_post(
    post_id: PositiveInt,
    _access: Access = Depends(require_browser_mutation),
    payload: PostMutation = Depends(_post_mutation_body),
):
    values = payload.model_dump()
    version = values.pop("version")
    return _post_operation(lambda: _post_storage().update(post_id, version, **values))


@app.delete("/api/admin/posts/{post_id}")
def delete_admin_post(
    post_id: PositiveInt,
    _access: Access = Depends(require_browser_mutation),
    payload: PostVersion = Depends(_post_version_body),
):
    return _post_operation(lambda: _post_storage().delete(post_id, payload.version))


@app.get("/api/admin/posts/{post_id}/revisions")
def list_admin_post_revisions(post_id: PositiveInt, _access: Access = Depends(require_browser_read)):
    document = _post_operation(lambda: _post_storage().get(post_id))
    if document is None:
        raise HTTPException(404, "post not found")
    return _post_operation(lambda: _post_storage().revisions(post_id))


@app.post("/api/admin/posts/{post_id}/revisions/{revision_id}/restore")
def restore_admin_post_revision(
    post_id: PositiveInt,
    revision_id: PositiveInt,
    _access: Access = Depends(require_browser_mutation),
    payload: PostVersion = Depends(_post_version_body),
):
    return _post_operation(lambda: _post_storage().restore(post_id, revision_id, payload.version))


@app.api_route("/readyz", methods=["GET", "HEAD"])
async def readyz(request: Request):
    ready, reason = await check_readiness(
        timeline_store, EXPECTED_SCHEMA_REVISIONS, _admin_config().valid
    )
    if not ready:
        return JSONResponse({"status": "not_ready", "reason": reason or "dependency unavailable"}, status_code=503)
    if request.app.state.machine_auth_configured and not _machine_config(request).valid:
        return JSONResponse({"status": "not_ready", "reason": "authentication configuration invalid"}, status_code=503)
    if request.app.state.media_config.partial:
        return JSONResponse({"status": "not_ready", "reason": "media configuration invalid"}, status_code=503)
    try:
        conflicts = await asyncio.to_thread(post_catalog.conflicts)
    except CatalogUnavailable:
        return JSONResponse({"status": "not_ready", "reason": "post catalog unavailable"}, status_code=503)
    if conflicts:
        return JSONResponse({"status": "not_ready", "reason": f"post catalog conflict: {conflicts[0]}"}, status_code=503)
    return {"status": "ok"}

def split_frontmatter(text: str) -> tuple[dict | None, str]:
    match = re.match(r"^---\r?\n(.*?)(?:\r?\n)---(?:\r?\n|$)", text, re.S)
    if not match:
        if text.startswith(("---\n", "---\r\n")):
            raise ValueError("missing frontmatter delimiter")
        return None, text
    try:
        metadata = yaml.safe_load(match[1]) or {}
    except Exception as exc:
        raise ValueError("invalid frontmatter") from exc
    if not isinstance(metadata, dict):
        raise ValueError("invalid frontmatter")
    return metadata, text[match.end():]


def parse_date(value) -> str | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        for format in ("%Y-%m-%d", "%m/%d/%Y"):
            try:
                return datetime.strptime(value, format).date().isoformat()
            except ValueError:
                pass
    return None


def _is_scalar_text(value: str) -> bool:
    return not any("\ud800" <= char <= "\udfff" for char in value)


def parse_tags(value) -> list[str] | None:
    """`tags: [a, b]` or `tags: a`, lowercased and deduplicated; None if invalid."""
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    if any(item is None or isinstance(item, (list, dict)) for item in items):
        return None
    tags = [t for item in items if (t := str(item).strip().lower())]
    if any(not _is_scalar_text(tag) for tag in tags):
        return None
    return list(dict.fromkeys(tags))


def load(path: Path, draft: bool = False) -> dict | None:
    m = FILENAME.match(path.name)
    if not m:
        return None
    slug = m[1]
    try:
        metadata, body = split_frontmatter(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        logger.warning("invalid frontmatter in %s", path.name)
        return None
    if metadata is None:
        if not draft:
            logger.warning("invalid frontmatter in %s", path.name)
            return None
        metadata = {}

    if "title" in metadata:
        title = metadata["title"]
        if title is None or isinstance(title, (list, dict)):
            logger.warning("invalid post metadata in %s", path.name)
            return None
        title = str(title).strip()
    else:
        title = slug if draft else ""
    if not title or not _is_scalar_text(title):
        logger.warning("invalid post metadata in %s", path.name)
        return None

    parsed_date = parse_date(metadata.get("date"))
    if not draft and parsed_date is None:
        logger.warning("invalid post metadata in %s", path.name)
        return None
    if "excerpt" in metadata:
        excerpt = metadata["excerpt"]
        if excerpt is None or isinstance(excerpt, (list, dict)):
            logger.warning("invalid post metadata in %s", path.name)
            return None
        excerpt = str(excerpt)
        if not _is_scalar_text(excerpt):
            logger.warning("invalid post metadata in %s", path.name)
            return None
    else:
        excerpt = None
    tags = parse_tags(metadata.get("tags"))
    if tags is None:
        logger.warning("invalid post metadata in %s", path.name)
        return None
    return {
        "slug": slug, "date": parsed_date, "title": title, "excerpt": excerpt, "tags": tags, "body": body, "draft": draft,
    }


def _published_file_slugs() -> set[str]:
    return {post["slug"] for path in POSTS_DIR.glob("*.md") if (post := load(path))}


def all_posts() -> list[dict]:
    posts = [p for f in POSTS_DIR.glob("*.md") if (p := load(f))]
    if os.environ.get("SHOW_DRAFTS") == "1":
        published_slugs = {p["slug"] for p in posts}
        # Published posts win when a draft uses the same slug.
        posts += [
            p for f in DRAFTS_DIR.glob("*.md")
            if (p := load(f, draft=True)) and p["slug"] not in published_slugs
        ]
    return sorted(posts, key=lambda p: (p["date"] is None, p["date"] or ""), reverse=True)


# A paragraph holding only an image with a title, ![alt](src "caption"), becomes a figure with that caption.
CAPTIONED = re.compile(r'<p>(<img [^>]*?title="([^"]*)"[^>]*>)</p>')


def caption_figures(html: str) -> str:
    return CAPTIONED.sub(r"<figure>\1<figcaption>\2</figcaption></figure>", html)


def render_post(body: str) -> str:
    return caption_figures(md.render(body, {"heading_anchors": True}))


def _catalog_unavailable(exc: CatalogUnavailable) -> HTTPException:
    logger.warning("post catalog unavailable: %s", type(exc.__cause__).__name__)
    return HTTPException(503, "posts unavailable")


def _list_posts_data(tag: str | None = None) -> list[dict]:
    try:
        posts = post_catalog.list_public(tag)
    except CatalogUnavailable as exc:
        raise _catalog_unavailable(exc) from exc
    return [{k: post[k] for k in ("slug", "date", "title", "excerpt", "tags", "draft")} for post in posts]


def _get_post_data(slug: str) -> dict | PostRedirect | None:
    try:
        post = post_catalog.get_public(slug)
    except CatalogUnavailable as exc:
        raise _catalog_unavailable(exc) from exc
    if not isinstance(post, dict):
        return post
    body = post.pop("body")
    return {**post, "html": render_post(body)}


def _json_etag_response(request: Request, content) -> Response:
    return etag_response(
        request,
        json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode(),
        "application/json",
    )


@app.api_route("/api/posts", methods=["GET", "HEAD"])
def list_posts(request: Request):
    return _json_etag_response(request, _list_posts_data())


@app.api_route("/api/posts/{slug}", methods=["GET", "HEAD"])
def get_post(request: Request, slug: str):
    post = _get_post_data(slug)
    if isinstance(post, PostRedirect):
        return RedirectResponse(f"/api/posts/{post.slug}", status_code=301)
    if post is None:
        raise HTTPException(404, "post not found")
    return _json_etag_response(request, post)


@app.api_route("/now", methods=["GET", "HEAD"])
@app.api_route("/now/", methods=["GET", "HEAD"])
def old_now():
    return RedirectResponse("/timeline", status_code=301)


@app.api_route("/favicon.ico", methods=["GET", "HEAD"])
def old_favicon():
    return RedirectResponse("/favicon.svg", status_code=301)


# check_dir=False: the app still starts when POSTS_DIR has no images folder.
app.mount("/images", StaticFiles(directory=POSTS_DIR / "images", check_dir=False), name="images")


def contained_file(root: Path, path: str) -> Path | None:
    """The file at root/path, or None if it is missing or resolves (via .., an absolute path, or a symlink) outside root."""
    root = root.resolve()
    requested = (root / path).resolve()
    try:
        requested.relative_to(root)
    except ValueError:
        return None
    return requested if requested.is_file() else None


def project_root(project: str) -> Path:
    root = CODE_DIR / project
    if not PROJECT.fullmatch(project) or not root.is_dir():
        raise HTTPException(404, "project not found")
    return root.resolve()


def project_file(project: str, path: str) -> Path:
    file = contained_file(project_root(project), path)
    if not file:
        raise HTTPException(404, "file not found")
    return file


def project_files(root: Path) -> list[str]:
    return sorted(
        rel for p in root.rglob("*")
        if contained_file(root, rel := p.relative_to(root).as_posix())
    )


def code_path_exists(project: str, path: str) -> bool:
    files = project_files(project_root(project))
    return not path or path in files or any(file.startswith(path + "/") for file in files)


def rewrite_readme_link(project: str, url: str, prefix: str) -> str | None:
    target = posixpath.normpath(posixpath.join(".", url))
    if posixpath.isabs(target) or target == ".." or target.startswith("../"):
        return None
    return f"{prefix}/{project}/{target}"


def _demote_readme_heading_tokens(tokens):
    for token in tokens:
        if token.type not in {"heading_open", "heading_close"}:
            continue
        source = token.tag[1]
        token.tag = f"h{min(int(source) + 1, 6)}"
        if token.type == "heading_open":
            token.attrSet("class", f"readme-h{source}")


def readme_html(project: str, root: Path) -> str | None:
    readme = contained_file(root, "README.md")
    if not readme:
        return None
    tokens = md.parse(readme.read_text(encoding="utf-8-sig"))
    # The project name in the page crumbs is the page's one h1, so README headings sit one level below it.
    _demote_readme_heading_tokens(tokens)
    # Relative README links point into the snapshot: images load raw bytes, links open the code view.
    for token in tokens:
        for child in token.children or []:
            attr = "src" if child.type == "image" else "href" if child.type == "link_open" else None
            url = child.attrGet(attr) if attr else None
            if url and not re.match(r"^([a-z][a-z0-9+.-]*:|/|#)", url, re.I):
                prefix = "/code-raw" if attr == "src" else "/code"
                rewritten = rewrite_readme_link(project, url, prefix)
                if rewritten:
                    child.attrSet(attr, rewritten)
                else:
                    child.attrs.pop(attr, None)
    return md.renderer.render(tokens, md.options, {})


@app.api_route("/api/code/{project}", methods=["GET", "HEAD"])
def get_project(project: str):
    root = project_root(project)
    files = project_files(root)
    return {"name": project, "files": files, "readme_html": readme_html(project, root)}


@app.api_route("/api/code/{project}/file/{path:path}", methods=["GET", "HEAD"])
def get_code_file(project: str, path: str):
    file = project_file(project, path)
    data = file.read_bytes()
    try:
        code = data.decode("utf-8")
    except UnicodeDecodeError:
        code = None
    if code is None or "\0" in code:  # NUL: git's binary check, catches ASCII-only data files
        return {"path": path, "binary": True}
    try:
        lexer = get_lexer_for_filename(file.name, code)
    except ClassNotFound:
        lexer = TextLexer()
    # Pygments closes its spans at each line end, so every line can be wrapped for CSS line numbers.
    lines = pygments_highlight(code, lexer, HtmlFormatter(nowrap=True)).splitlines()
    return {"path": path, "html": "".join(f'<span class="line">{line}</span>\n' for line in lines)}


@app.api_route("/code-raw/{project}/{path:path}", methods=["GET", "HEAD"])
def get_code_raw(project: str, path: str):
    # sandbox: a raw SVG or HTML file opened directly cannot run scripts on the site origin.
    return FileResponse(project_file(project, path), headers={"Content-Security-Policy": "sandbox"})


@app.api_route("/media/{asset_id}/{variant}", methods=["GET", "HEAD"])
def get_media(asset_id: str, variant: str, request: Request):
    # One 404 for every miss (bad id, bad variant, unattached, deleted), so the route is no existence oracle.
    not_found = HTTPException(404, "not found")
    name = {f"{v}.webp": v for v in VARIANTS}.get(variant)
    try:
        parsed = uuid.UUID(asset_id)
    except ValueError:
        raise not_found from None
    if str(parsed) != asset_id or name is None:  # one canonical URL per object
        raise not_found
    store, assets = media_store, timeline_store
    if store is None or assets is None:
        raise HTTPException(503, "media unavailable")
    try:
        if not assets.is_in_use(parsed):
            raise not_found
        data = store.get(media_key(parsed, name))
    except HTTPException:
        raise
    except MediaNotFound:
        raise not_found from None
    except Exception as exc:
        logger.warning("media.serve.unavailable: %s", type(exc).__name__)
        raise HTTPException(503, "media unavailable") from exc
    headers = {"Cache-Control": "public, max-age=31536000, immutable", "Content-Length": str(len(data))}
    # shortcut: HEAD still reads the object to know its length; add a store head() if HEAD traffic matters.
    return Response(b"" if request.method == "HEAD" else data, media_type=CONTENT_TYPE, headers=headers)


from discovery import router as discovery_router

app.include_router(discovery_router)


@app.api_route("/{path:path}", methods=["GET", "HEAD"])
def frontend(path: str, request: Request):
    if path == "api" or path.startswith("api/"):
        raise HTTPException(404, "not found")
    headers = {"X-Robots-Tag": "noindex"} if routes.noindex(path) else None
    root = FRONTEND_DIST.resolve()
    index = contained_file(root, "index.html")
    if not index:
        raise HTTPException(404, "not found", headers=headers)
    # One redirect to the one URL form, files included: extra slashes go, and /index.html is /.
    target = routes.canonical("/" + path)
    file = contained_file(root, path)
    if target != "/" + path or file == index:
        query = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(quote("/" if file == index else target, safe="/") + query, status_code=301, headers=headers)
    if file:
        if path.startswith("assets/"):
            # Built public files only: the admin preview's sandbox (an opaque origin) loads its fonts and scripts by CORS.
            headers = {**(headers or {}), "Access-Control-Allow-Origin": "*"}
        return FileResponse(file, headers=headers)
    match = routes.match(target)
    if match and match[0] == "post":
        request.state.resolved_post = _get_post_data(match[1]["slug"])
        if isinstance(request.state.resolved_post, PostRedirect):
            return RedirectResponse(f"/posts/{request.state.resolved_post.slug}", status_code=301)
    preload, head, markup, status_code = render_page(match, request)
    if status_code == 404:
        headers = {"X-Robots-Tag": "noindex"}
    content = fill_index(index.read_text(encoding="utf-8"), preload, head, markup, target).encode()
    return etag_response(request, content, "text/html", headers, status_code)


def render_page(match: tuple[str, dict[str, str]] | None, request: Request) -> tuple[dict, dict, str, int]:
    """Preload data (with the page the React app renders), head tags, no-JS markup and status for a matched path."""
    if match is None:
        return not_found_page("not found")
    kind, params = match
    preload, head, markup, status_code = RENDERERS[kind](request, **params)
    # A renderer that answers not-found sets its own page.
    return {"page": {"kind": kind, **params}} | preload, head, markup, status_code


def home_page(request: Request) -> tuple[dict, dict, str, int]:
    posts = _list_posts_data()
    return {"/api/posts": posts}, {"title": "neenerweener"}, home_markup(posts, request.query_params.get("tag")), 200


def timeline_page_data(request: Request) -> tuple[dict, dict, str, int]:
    before = timeline_before(request)
    try:
        profile = get_profile()
    except HTTPException:
        profile = _public_profile(DEFAULT_PROFILE)
    page, markup, status_code = timeline_page(before, profile)
    return {"/api/timeline": page, "/api/profile": profile}, {"title": "timeline", "description": "timeline"}, markup, status_code


def canvas_markup(events: list[dict]) -> str:
    activity = "".join(
        f'<li>revision {escape(str(event["revision"]))}: '
        f'{"cleared a region" if event.get("event_kind") == "clear" else "pixel updated"}'
        f'{" by " + str(event["agent"]) if event.get("agent") else " by " + str(event["fingerprint_suffix"]) if event.get("fingerprint_suffix") else ""}</li>'
        for event in events
    )
    if not activity:
        activity = '<li class="muted">no activity yet</li>'
    return (
        '<main><article class="canvas-page"><h1>canvas</h1>'
        '<p class="muted">a shared 64 × 64 pixel canvas</p>'
        '<div class="canvas-frame"><img src="/api/canvas.png" width="512" height="512" '
        'alt="current canvas" decoding="async"></div>'
        '<div class="canvas-controls"><button type="button">replay</button></div>'
        '<p role="status" aria-live="polite"></p>'
        '<section aria-labelledby="canvas-activity"><h2 id="canvas-activity">recent activity</h2>'
        f'<ol class="canvas-activity">{activity}</ol></section></article>{site_footer("canvas")}</main>'
    )


def canvas_page_data(request: Request) -> tuple[dict, dict, str, int]:
    try:
        revision, pixels = awaitable_canvas_snapshot()
        events = [_canvas_event_json(event) for event in _canvas_store().events(max(0, revision - 20), 20)["events"]]
    except HTTPException:
        revision, pixels, events = 0, bytes([255] * (64 * 64)), []
    snapshot = snapshot_payload(revision, pixels)
    return {"/api/canvas": snapshot, "/api/canvas/events": {"reset": False, "revision": revision, "events": events}}, {"title": "canvas", "description": "canvas"}, canvas_markup(events), 200


def _canvas_event_json(event: dict) -> dict:
    return {key: value.isoformat() if isinstance(value, datetime) else str(value) if key == "agent_id" and value is not None else value for key, value in event.items()}


def awaitable_canvas_snapshot() -> tuple[int, bytes]:
    return _canvas_store().snapshot()


def timeline_before(request: Request) -> int | None:
    values = request.query_params.getlist("before")
    if not values:
        return None
    if len(values) != 1 or not values[0].isdigit() or int(values[0]) <= 0:
        raise HTTPException(400, "invalid timeline cursor")
    return int(values[0])


def admin_page(request: Request, section: str = "") -> tuple[dict, dict, str, int]:
    # Signed out, the sign-in form is the page; /admin is no-store, so a per-visitor title is safe here.
    label = routes.ADMIN_PAGES[section] if request.cookies.get(ADMIN_HINT_COOKIE) else "sign in"
    return {}, {"title": f"{label} · admin", "robots": "noindex"}, ADMIN_SHELL, 200


# Mirrors the admin Suspense fallback in App.tsx, so the first paint is already the shell React renders.
ADMIN_SHELL = (
    '<main><article class="admin"><div class="admin-head"><h1>admin</h1></div>'
    '<noscript><p class="muted">the admin panel needs JavaScript</p></noscript></article>'
    f"{site_footer(None)}</main>"
)


def post_page_data(request: Request, slug: str) -> tuple[dict, dict, str, int]:
    post = getattr(request.state, "resolved_post", None)
    if post is None:
        return not_found_page("post not found", back=True)
    return {f"/api/posts/{slug}": post}, {"title": post["title"], "description": post["excerpt"] or summary(post["html"]), "published": post["date"]}, post_markup(post), 200


def code_page_data(request: Request, project: str, path: str = "") -> tuple[dict, dict, str, int]:
    try:
        if not code_path_exists(project, path):
            return not_found_page("file not found")
    except HTTPException as exc:
        return not_found_page(str(exc.detail))
    root = project_root(project)
    title = f"{project}" if not path else f"{path} · {project}"
    readme = readme_html(project, root)
    description = summary(readme) if readme else None
    head = {"title": title}
    if description:
        head["description"] = description
    return {}, head, "", 200


RENDERERS = {"home": home_page, "timeline": timeline_page_data, "canvas": canvas_page_data, "admin": admin_page, "post": post_page_data, "code": code_page_data}


def not_found_page(label: str, back: bool = False) -> tuple[dict, dict, str, int]:
    """The one not-found view; the React app reads it from the preload and renders the same markup."""
    heading = f'<h1 class="muted">{escape(label)}</h1>'
    heading = f'<div class="has-back"><a class="back" href="/" aria-label="back">←</a>{heading}</div>' if back else f'{heading}<a href="/">neenerweener</a>'
    markup = f'<main>{heading}{site_footer(None)}</main>'
    page = {"kind": "not-found", "label": label, "back": back}
    return {"page": page}, {"title": "not found", "robots": "noindex"}, markup, 404


def timeline_page(before: int | None, profile: dict) -> tuple[dict | None, str, int]:
    try:
        page = list_timeline(before=before, limit=20)
    except HTTPException:
        return None, timeline_markup(None, profile), 503
    except Exception:
        logger.warning("timeline unavailable during page render")
        return None, timeline_markup(None, profile), 503
    return page, timeline_markup(page, profile), 200


def summary(html: str, limit: int = 200) -> str:
    """The start of the first paragraph as plain text, for link previews."""
    first = re.search(r"<p>(.*?)</p>", html, re.S)
    text = re.sub(r"<[^>]+>", "", first[1]) if first else ""
    text = " ".join(unescape(text).split())
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + "…"


def timeline_markup(page: dict | None, profile: dict) -> str:
    """The first timeline page as the client renders it; None means the timeline is unavailable."""
    entries = page["entries"] if page else []
    if page is None:
        content = '<p class="muted">could not load timeline</p>'
    elif not entries and page["next_cursor"] is None:
        content = '<p class="muted">nothing here yet</p>'
    else:
        items = []
        avatar = avatar_markup(profile)
        name = f'<span class="name">{escape(profile["display_name"], quote=False)}</span>'
        if profile["handle"]:
            name += f'<span class="handle">@{escape(profile["handle"], quote=False)}</span>'
        for entry in entries:
            edited = ""
            if entry.get("edited_at"):
                edited_full = escape(full_timestamp(entry["edited_at"], entry["edited_zone"]))
                edited = (
                    f'<span aria-hidden="true">·</span><span class="edited" title="{edited_full}">'
                    f'edited<span class="sr-only"> {edited_full}</span></span>'
                )
            created = entry["created_at"]
            created_utc = _utc_datetime(created).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            created_full = escape(full_timestamp(created, entry["created_zone"]))
            items.append(
                f'<article class="timeline-entry" id="entry-{entry["id"]}">{avatar}<div class="entry-main">'
                f'<header>{name}<span class="when"><span aria-hidden="true">·</span>'
                f'<a href="#entry-{entry["id"]}" aria-label="{created_full}">'
                f'<time datetime="{escape(created_utc)}" title="{created_full}">{compact_timestamp(created, page["now"])}</time>'
                f'</a>{edited}</span></header>'
                f'<div class="entry-body">{entry["html"]}</div>'
                f'{media_markup(entry["media"])}'
                "</div></article>"
            )
        content = f'<div class="timeline">{"".join(items)}</div>'
    if page is not None and page["next_cursor"] is not None:
        cursor = page["next_cursor"]
        content += f'<div class="load-more"><a href="/timeline?before={cursor}">load more</a></div>'
    if page is not None:
        content += '<p class="sr-only" role="status"></p>'  # the React page's always-mounted status region
    return f'<main><article><h1>timeline</h1>{content}</article>{site_footer("timeline")}</main>'


# A single image keeps its shape up to this height (CSS px); 2 to 4 images fill a grid of fixed cell shapes.
SINGLE_IMAGE_MAX_HEIGHT = 512
# Rendered widths for the sizes hints, from frontend/src/index.css (test_media_sizes_follow_the_css checks them):
# 1rem is 15px, main is at most 52rem wide with 1.25rem side padding, images sit right of the 44px avatar and
# its 12px gap, a single image is capped at 100% - 2px, and the 2 to 4 image grid has a 1px border and a 2px
# gap between its two columns.
PAGE_MAX_WIDTH = 52 * 15  # 780: above this the column stops growing
SINGLE_GUTTER = 2 * 1.25 * 15 + 44 + 12 + 2  # 95.5: window width minus a single image's widest box
GRID_SIZES = "(max-width: 780px) calc(50vw - 48.75px), 341.25px"  # (window - 97.5px) / 2, capped at (780 - 97.5) / 2


def avatar_markup(profile: dict) -> str:
    """The poster's 44px picture, or a circle with the initial; Avatar in App.tsx renders the same markup."""
    if not profile["avatar_id"]:
        return f'<div class="avatar" aria-hidden="true">{escape(profile["avatar_initial"], quote=False)}</div>'
    base = f'/media/{escape(profile["avatar_id"])}'
    return (
        f'<img class="avatar" sizes="44px" width="44" height="44" alt="" aria-hidden="true" '
        f'srcset="{base}/sm.webp 96w, {base}/lg.webp 192w" src="{base}/sm.webp">'
    )


def media_markup(media: list[dict]) -> str:
    """An entry's images like a tweet's; EntryMedia in App.tsx renders the same markup."""
    if not media:
        return ""
    items = []
    for item in media:
        width, height = item["width"], item["height"]
        base = f'/media/{escape(item["id"])}'
        sm_width = fit(width, height, SM_EDGE)[0]
        srcset = f"{base}/sm.webp {sm_width}w" + (f", {base}/lg.webp {width}w" if sm_width < width else "")
        if len(media) == 1:
            # The width and height attributes reserve the box before the image loads, so nothing shifts.
            if height > SINGLE_IMAGE_MAX_HEIGHT:
                width, height = max(1, round(width * SINGLE_IMAGE_MAX_HEIGHT / height)), SINGLE_IMAGE_MAX_HEIGHT
            # It shows at min(width, window - gutter), so the window width where the cap starts is the breakpoint.
            sizes = (
                f"(max-width: {min(width + SINGLE_GUTTER, PAGE_MAX_WIDTH)}px) calc(100vw - {SINGLE_GUTTER}px), "
                f"{min(width, PAGE_MAX_WIDTH - SINGLE_GUTTER)}px"
            )
        else:
            sizes = GRID_SIZES
        label = "" if item["alt"] else ' aria-label="open image"'
        items.append(
            # React writes srcset and src last; the same order here keeps the two markups equal.
            f'<a href="{base}/lg.webp"{label}><img sizes="{sizes}" width="{width}" height="{height}" '
            f'alt="{escape(item["alt"])}" loading="lazy" decoding="async" srcset="{srcset}" src="{base}/sm.webp"></a>'
        )
    return f'<div class="media media-{len(media)}">{"".join(items)}</div>'


def _utc_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.astimezone(timezone.utc)


def compact_timestamp(value: str, now: str) -> str:
    """Age against the page's one now: "now", "12m", "3h", then the Toronto date, with the year if not now's."""
    moment, current = _utc_datetime(value), _utc_datetime(now)
    seconds = (current - moment).total_seconds()
    if seconds < 60:
        return "now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    local = moment.astimezone(TORONTO)
    year = "" if local.year == current.astimezone(TORONTO).year else f", {local.year}"
    return f"{local.strftime('%b').lower()} {local.day}{year}"


def full_timestamp(value: str, zone: str) -> str:
    """Toronto wall time; zone is the serializer's EST or EDT for this instant."""
    return _utc_datetime(value).astimezone(TORONTO).strftime(f"%B %-d, %Y at %-I:%M %p {zone}")


def post_markup(post: dict) -> str:
    # Mirrors PostPage in App.tsx so the swap to the React page is not visible.
    tags = "".join(f'<a href="/?tag={quote(t)}">#{escape(t)}</a>' for t in post["tags"])
    return (
        '<main><article><div class="has-back post-head"><a class="back" href="/" aria-label="back">←</a>'
        f'<h1>{escape(post["title"])}</h1></div><div class="meta"><span class="tags">{tags}</span></div>'
        f'<div>{post["html"]}</div></article>{site_footer(None)}</main>'
    )


def fill_index(index: str, preload: dict, head: dict, markup: str, canonical_path: str) -> str:
    from head import head_tags

    tags = head_tags(preload["page"]["kind"], head, canonical_path)
    tags += ['<link rel="alternate" type="application/atom+xml" href="/feed.xml" title="Atom feed" />']
    # "<" escaped so post text cannot close the script tag.
    data = json.dumps(preload).replace("<", "\\u003c")
    tags.append(f'<script id="preload" type="application/json">{data}</script>')
    index = re.sub(r"<title>.*?</title>", "", index, count=1, flags=re.S)
    index = index.replace("</head>", "\n".join(tags) + "\n</head>", 1)
    return re.sub(r'<div id="root">\s*</div>', lambda _: f'<div id="root">{markup}</div>', index, count=1)
