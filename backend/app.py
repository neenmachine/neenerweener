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
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from markdown_it import MarkdownIt
from mdit_py_plugins.dollarmath import dollarmath_plugin
from pydantic import BaseModel, ConfigDict, ValidationError
from pygments import highlight as pygments_highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import TextLexer, get_lexer_by_name, get_lexer_for_filename
from pygments.util import ClassNotFound

from anchors import add_heading_anchors
from operations import check_readiness, configure_logging, load_schema_heads
from page_markup import home_markup, site_footer
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
    store_image,
    sweep,
)
from timeline import (
    InvalidAttachment,
    TimelineStore,
    TimelineStoreProtocol,
    serialize_entry,
    validate_timeline_body,
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


app = FastAPI(lifespan=lifespan)
app.state.machine_auth_configured = False
app.state.machine_auth_config = MachineAuthConfig({}, None, None, False)
app.state.media_config = MediaConfig()
configure_logging()
timeline_store: TimelineStoreProtocol | None = (
    TimelineStore(os.environ["DATABASE_URL"]) if os.environ.get("DATABASE_URL") else None
)
auth_store: AuthStore | None = AuthStore(os.environ["DATABASE_URL"]) if os.environ.get("DATABASE_URL") else None
machine_auth_store: MachineAuthStore | None = (
    MachineAuthStore(os.environ["DATABASE_URL"]) if os.environ.get("DATABASE_URL") else None
)
media_store: MediaStore | None = None  # set at startup when all MEDIA_* variables are present
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
    if request.url.path.startswith(("/api/admin/", "/api/cli/auth/")):
        response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), geolocation=(), microphone=(), payment=(), usb=()"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Content-Security-Policy-Report-Only"] = (
        "default-src 'self'; script-src 'self'; connect-src 'self'; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    )
    return response


@app.api_route("/healthz", methods=["GET", "HEAD"])
async def healthz():
    return {"status": "ok"}


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
    }


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str


class TimelineMutation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body_markdown: str


class Attachment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: uuid.UUID
    alt: str = ""


class TimelineCreate(TimelineMutation):
    """Attachments are set once at create; an edit (TimelineMutation) cannot change them."""

    attachments: list[Attachment] = []


MAX_TIMELINE_JSON_BYTES = 65_536  # body plus four alt texts; matches the CLI's JSON cap


class ChallengeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key_fingerprint: str


class TokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    challenge_id: uuid.UUID
    signature: str


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


# A non-secret hint that tells /timeline whether to check the session; never used for authorization.
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
    global _media_sweep_due
    if time.monotonic() >= _media_sweep_due:
        _media_sweep_due = time.monotonic() + MEDIA_SWEEP_INTERVAL_SECONDS
        background.add_task(_sweep_media, assets, store)  # after the response, so a slow bucket cannot hold it
    return {"id": str(asset_id), "width": image.width, "height": image.height}


async def _sweep_media(assets, store) -> None:
    try:
        await asyncio.to_thread(sweep, assets, store)
    except Exception as exc:
        logger.warning("media.sweep.unavailable: %s", type(exc).__name__)


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
    try:
        validate_timeline_body(payload.body_markdown)
    except ValueError as exc:
        raise HTTPException(422, "invalid timeline entry") from exc
    try:
        entry = _timeline_storage().update(entry_id, payload.body_markdown)
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


def render_post(body: str) -> str:
    html = md.render(body, {"heading_anchors": True})
    return CAPTIONED.sub(r"<figure>\1<figcaption>\2</figcaption></figure>", html)


@app.api_route("/api/posts", methods=["GET", "HEAD"])
def list_posts():
    return [{k: p[k] for k in ("slug", "date", "title", "excerpt", "tags", "draft")} for p in all_posts()]


@app.api_route("/api/posts/{slug}", methods=["GET", "HEAD"])
def get_post(slug: str):
    for p in all_posts():
        if p["slug"] == slug:
            body = p.pop("body")
            return {**p, "html": render_post(body)}
    raise HTTPException(404, "post not found")


@app.api_route("/now", methods=["GET", "HEAD"])
@app.api_route("/now/", methods=["GET", "HEAD"])
def old_now():
    return RedirectResponse("/timeline", status_code=301)


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
        if not assets.is_attached(parsed):
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
        return FileResponse(file, headers=headers)
    preload, head, markup, status_code = render_page(routes.match(target), request)
    return HTMLResponse(fill_index(index.read_text(encoding="utf-8"), preload, head, markup), status_code=status_code, headers=headers)


def render_page(match: tuple[str, dict[str, str]] | None, request: Request) -> tuple[dict, dict, str, int]:
    """Preload data (with the page the React app renders), head tags, no-JS markup and status for a matched path."""
    if match is None:
        return not_found_page("not found")
    kind, params = match
    preload, head, markup, status_code = RENDERERS[kind](request, **params)
    # A renderer that answers not-found sets its own page.
    return {"page": {"kind": kind, **params}} | preload, head, markup, status_code


def home_page(request: Request) -> tuple[dict, dict, str, int]:
    posts = list_posts()
    return {"/api/posts": posts}, {"title": "neenerweener"}, home_markup(posts, request.query_params.get("tag")), 200


def timeline_page_data(request: Request) -> tuple[dict, dict, str, int]:
    page, markup, status_code = timeline_page(timeline_before(request))
    return {"/api/timeline": page}, {"title": "timeline", "description": "timeline"}, markup, status_code


def timeline_before(request: Request) -> int | None:
    values = request.query_params.getlist("before")
    if not values:
        return None
    if len(values) != 1 or not values[0].isdigit() or int(values[0]) <= 0:
        raise HTTPException(400, "invalid timeline cursor")
    return int(values[0])


def admin_page(request: Request) -> tuple[dict, dict, str, int]:
    return {}, {"title": "admin", "robots": "noindex"}, ADMIN_SHELL, 200


# Mirrors the admin Suspense fallback in App.tsx, so the first paint is already the shell React renders.
ADMIN_SHELL = (
    '<main><article class="admin"><div class="admin-head"><h1>admin</h1></div>'
    '<noscript><p class="muted">the admin panel needs JavaScript</p></noscript></article>'
    f"{site_footer(None)}</main>"
)


def post_page_data(request: Request, slug: str) -> tuple[dict, dict, str, int]:
    try:
        post = get_post(slug)
    except HTTPException:
        return not_found_page("post not found", back=True)
    return {f"/api/posts/{slug}": post}, {"title": post["title"], "description": post["excerpt"] or summary(post["html"])}, post_markup(post), 200


def code_page_data(request: Request, project: str, path: str = "") -> tuple[dict, dict, str, int]:
    try:
        if not code_path_exists(project, path):
            return not_found_page("file not found")
    except HTTPException as exc:
        return not_found_page(str(exc.detail))
    return {}, {"title": "neenerweener"}, "", 200


RENDERERS = {"home": home_page, "timeline": timeline_page_data, "admin": admin_page, "post": post_page_data, "code": code_page_data}


def not_found_page(label: str, back: bool = False) -> tuple[dict, dict, str, int]:
    """The one not-found view; the React app reads it from the preload and renders the same markup."""
    heading = f'<h1 class="muted">{escape(label)}</h1>'
    heading = f'<div class="has-back"><a class="back" href="/" aria-label="back">←</a>{heading}</div>' if back else f'{heading}<a href="/">neenerweener</a>'
    markup = f'<main>{heading}{site_footer(None)}</main>'
    page = {"kind": "not-found", "label": label, "back": back}
    return {"page": page}, {"title": "not found", "robots": "noindex"}, markup, 404


def timeline_page(before: int | None) -> tuple[dict | None, str, int]:
    try:
        page = list_timeline(before=before, limit=20)
    except HTTPException:
        return None, timeline_markup(None), 503
    except Exception:
        logger.warning("timeline unavailable during page render")
        return None, timeline_markup(None), 503
    return page, timeline_markup(page), 200


def summary(html: str, limit: int = 200) -> str:
    """The start of the first paragraph as plain text, for link previews."""
    first = re.search(r"<p>(.*?)</p>", html, re.S)
    text = re.sub(r"<[^>]+>", "", first[1]) if first else ""
    text = " ".join(unescape(text).split())
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + "…"


def timeline_markup(page: dict | None) -> str:
    """The first timeline page as the client renders it; None means the timeline is unavailable."""
    entries = page["entries"] if page else []
    if page is None:
        content = '<p class="muted">could not load timeline</p>'
    elif not entries and page["next_cursor"] is None:
        content = '<p class="muted">nothing here yet</p>'
    else:
        items = []
        for entry in entries:
            edited = (
                f'<span class="edited" title="{escape(full_timestamp(entry["edited_at"]))}">'
                f'edited<span class="sr-only"> {escape(full_timestamp(entry["edited_at"]))}</span></span>'
                if entry.get("edited_at") else ""
            )
            created = entry["created_at"]
            created_utc = _utc_datetime(created).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            created_full = full_timestamp(created)
            items.append(
                f'<article class="timeline-entry" id="entry-{entry["id"]}">'
                f'<div>{entry["html"]}</div>'
                f'{media_markup(entry["media"])}'
                f'<footer><a href="#entry-{entry["id"]}" aria-label="{escape(created_full)}">'
                f'<time datetime="{escape(created_utc)}" title="{escape(created_full)}">{escape(compact_timestamp(created))}</time>'
                f'</a>{edited}</footer>'
                "</article>"
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
# 1rem is 15px, main is at most 52rem wide with 1.25rem side padding, a single image is capped at 100% - 2px,
# and the 2 to 4 image grid has a 1px border and a 2px gap between its two columns.
PAGE_MAX_WIDTH = 52 * 15  # 780: above this the column stops growing
SINGLE_GUTTER = 2 * 1.25 * 15 + 2  # 39.5: window width minus a single image's widest box
GRID_SIZES = "(max-width: 780px) calc(50vw - 20.75px), 369.25px"  # (column - 2px border - 2px gap) / 2


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


def compact_timestamp(value: str, today: date | None = None) -> str:
    current = today or datetime.now(timezone.utc).date()
    moment = _utc_datetime(value)
    year = "" if moment.date().year == current.year else f", {moment.year}"
    return f"{moment.strftime('%b').lower()} {moment.day}{year}"


def full_timestamp(value: str) -> str:
    moment = _utc_datetime(value)
    return moment.strftime('%B %-d, %Y at %-I:%M %p')


def post_markup(post: dict) -> str:
    # Mirrors PostPage in App.tsx so the swap to the React page is not visible.
    tags = "".join(f'<a href="/?tag={quote(t)}">#{escape(t)}</a>' for t in post["tags"])
    return (
        '<main><article><div class="has-back post-head"><a class="back" href="/" aria-label="back">←</a>'
        f'<h1>{escape(post["title"])}</h1></div><div class="meta"><span class="tags">{tags}</span></div>'
        f'<div>{post["html"]}</div></article>{site_footer(None)}</main>'
    )


def fill_index(index: str, preload: dict, head: dict, markup: str) -> str:
    tags = [f"<title>{escape(head['title'])}</title>", f'<meta property="og:title" content="{escape(head["title"])}" />',
            '<meta property="og:site_name" content="neenerweener" />',
            '<link rel="alternate" type="application/atom+xml" href="/feed.xml" title="Atom feed" />']
    if head.get("description"):
        tags += [f'<meta name="description" content="{escape(head["description"])}" />',
                 f'<meta property="og:description" content="{escape(head["description"])}" />']
    if head.get("robots"):
        tags.append(f'<meta name="robots" content="{escape(head["robots"])}">')
    # "<" escaped so post text cannot close the script tag.
    data = json.dumps(preload).replace("<", "\\u003c")
    tags.append(f'<script id="preload" type="application/json">{data}</script>')
    index = re.sub(r"<title>.*?</title>", "", index, count=1, flags=re.S)
    index = index.replace("</head>", "\n".join(tags) + "\n</head>", 1)
    return re.sub(r'<div id="root">\s*</div>', lambda _: f'<div id="root">{markup}</div>', index, count=1)
