import hashlib
from collections.abc import Mapping

from fastapi import Request, Response


NO_STORE_PREFIXES = ("/api/admin/", "/api/cli/auth/", "/api/agents/auth/", "/api/agents/me")


def etag_response(
    request: Request,
    content: bytes,
    media_type: str,
    headers: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    etag = f'"{hashlib.sha256(content).hexdigest()}"'
    response_headers = {**(headers or {}), "ETag": etag}
    candidates = {
        value.strip().removeprefix("W/").strip()
        for value in request.headers.get("if-none-match", "").split(",")
    }
    if etag in candidates or "*" in candidates:
        return Response(status_code=304, headers=response_headers)
    return Response(content=content, media_type=media_type, headers=response_headers, status_code=status_code)


def cache_control(path: str, status: int, response_headers: Mapping[str, str]) -> str | None:
    """Return the route policy, preserving response-specific directives where allowed."""
    if path.startswith(NO_STORE_PREFIXES) or path == "/readyz" or path == "/healthz" or path == "/admin" or path.startswith("/admin/"):
        return "no-store"
    if path == "/favicon.ico" and status in (301, 308):
        return "public, max-age=3600"
    if not (200 <= status < 300 or status == 304):
        return "no-cache"
    existing = response_headers.get("cache-control")
    if existing:
        return existing
    if path.startswith("/assets/"):
        return "public, max-age=31536000, immutable"
    if path.startswith(("/images/", "/code-raw/")) or path == "/favicon.svg":
        return "public, max-age=3600"
    content_type = response_headers.get("content-type", "")
    if content_type.startswith("text/html"):
        return "no-cache"
    if path.count("/") == 1 and "." in path.rsplit("/", 1)[1]:
        return "public, max-age=3600"
    if path.startswith("/api/"):
        return "no-cache"
    return None
