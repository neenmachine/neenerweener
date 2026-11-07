import hashlib
import hmac
import ipaddress

from fastapi import Request


def railway_client_ip(request: Request) -> str | None:
    values = request.headers.getlist("x-forwarded-for")
    if not values:
        return None
    candidate = values[-1].rsplit(",", 1)[-1].strip()
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if "%" in candidate or candidate.startswith("[") or candidate.endswith("]"):
        return None
    return address.compressed


def rate_limit_key(secret: bytes, domain: str, identity: str) -> bytes:
    message = b"ween-machine-rate-limit\0" + domain.encode("ascii") + b"\0" + identity.encode("ascii")
    return hmac.new(secret, message, hashlib.sha256).digest()
