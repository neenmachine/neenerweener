"""The minimal v0 timeline client."""
import argparse
import base64
import binascii
import ipaddress
import json
import os
import stat
import subprocess
import sys
import tempfile
import tomllib
import uuid
from datetime import datetime
from pathlib import Path
from re import fullmatch
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlunparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import __version__

MAX_RESPONSE = 1_048_576
TIMEOUT = 15
UPLOAD_TIMEOUT = 120
MAX_IMAGE_BYTES = 20 * 1024 * 1024
NAMESPACE = "neenerweener.ca/cli-auth/v1"

class CliError(Exception):
    @property
    def code(self):
        return ERROR_CODES[type(self)]


class UsageConfigError(CliError):
    pass


class SignerAuthError(CliError):
    pass


class ServerValidationError(CliError):
    pass


class ThrottleError(CliError):
    pass


class NetworkError(CliError):
    pass


ERROR_CODES = {
    UsageConfigError: 2,
    SignerAuthError: 3,
    ServerValidationError: 4,
    ThrottleError: 5,
    NetworkError: 1,
}


class CliParser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageConfigError(message)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

OPENER = build_opener(NoRedirect)


def _server(value):
    try:
        p = urlparse(value)
        port = p.port
        host = (p.hostname or "").lower().rstrip(".")
    except ValueError:
        raise UsageConfigError("invalid server URL") from None
    if port is not None and not 1 <= port <= 65535:
        raise UsageConfigError("invalid server URL")
    if not host or "@" in p.netloc:
        raise UsageConfigError("invalid server URL")
    if p.scheme not in ("http", "https") or not p.netloc or p.query or p.fragment:
        raise UsageConfigError("server must be an HTTPS URL (or HTTP localhost)")
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if p.scheme != "https" and not loopback:
        raise UsageConfigError("server must be HTTPS (HTTP is allowed only for localhost)")
    return urlunparse((p.scheme, p.netloc, p.path.rstrip("/"), "", "", ""))


def _config():
    try:
        path = Path("~/.config/ween/config.toml").expanduser()
        if not path.exists():
            return {}
        with path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, RuntimeError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise UsageConfigError("cannot read config") from None
    if not isinstance(data, dict) or set(data) - {"server", "key"}:
        raise UsageConfigError("config contains an unknown key")
    if any(not isinstance(v, str) for v in data.values()):
        raise UsageConfigError("config values must be strings")
    return data


def _key(path):
    try:
        p = Path(path).expanduser()
        st = p.lstat()
    except (OSError, RuntimeError):
        raise UsageConfigError("key file is not readable") from None
    if (stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode)
            or not (st.st_mode & 0o400) or st.st_mode & 0o077):
        raise UsageConfigError("key file must be a private regular owner-readable file")
    return p


def _run(args, stdin=None):
    # No timeout: ssh-keygen may wait for a passphrase on /dev/tty, and Ctrl-C ends the run cleanly.
    try:
        return subprocess.run(args, input=stdin, capture_output=True, check=False, shell=False)
    except FileNotFoundError:
        raise SignerAuthError("ssh-keygen not found") from None
    except OSError:
        raise SignerAuthError("cannot run ssh-keygen") from None


def _fingerprint(key):
    result = _run(["ssh-keygen", "-lf", str(key), "-E", "sha256"])
    try:
        fields = result.stdout.decode("utf-8", "strict").split()
    except UnicodeDecodeError:
        raise SignerAuthError("cannot read the SSH key fingerprint") from None
    if result.returncode or len(fields) < 2 or not fields[1].startswith("SHA256:"):
        raise SignerAuthError("cannot read the SSH key fingerprint")
    return fields[1]


def _signature(result):
    try:
        value = result.stdout.decode("utf-8", "strict")
    except UnicodeDecodeError:
        raise SignerAuthError("SSH signing failed; check that the key is loaded or its passphrase is available") from None
    match = fullmatch(
        r"-----BEGIN SSH SIGNATURE-----\n([A-Za-z0-9+/=\n]+)\n-----END SSH SIGNATURE-----\n?",
        value,
    )
    if result.returncode or not match:
        raise SignerAuthError("SSH signing failed; check that the key is loaded or its passphrase is available")
    try:
        decoded = base64.b64decode(match.group(1).replace("\n", ""), validate=True)
    except (binascii.Error, ValueError):
        raise SignerAuthError("SSH signing failed; check that the key is loaded or its passphrase is available") from None
    if not decoded.startswith(b"SSHSIG"):
        raise SignerAuthError("SSH signing failed; check that the key is loaded or its passphrase is available")
    return value


def _json_response(response):
    data = response.read(MAX_RESPONSE + 1)
    if len(data) > MAX_RESPONSE:
        raise NetworkError("server response is too large")
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise NetworkError("server returned invalid JSON")
    if not isinstance(value, dict):
        raise NetworkError("server returned an invalid response")
    return value


def _sensitive_values(payload, bearer, headers=()):
    values = {value for value in payload.values() if isinstance(value, str) and value}
    values.update(value for value in headers if isinstance(value, str) and value)
    if bearer:
        values.add(bearer)
    return values


def _safe_server_value(value, sensitive, limit):
    return (value if isinstance(value, str) and value and len(value) <= limit
            and not any(ord(char) < 32 or ord(char) == 127 for char in value)
            and not any(secret in value for secret in sensitive) else None)


def _http_error(error, sensitive, rejected):
    if error.code == 422:
        detail = rejected
        try:
            candidate = _json_response(error).get("detail")
            detail = _safe_server_value(candidate, sensitive, 120) or detail
        except CliError:
            pass
        return ServerValidationError(detail)
    if error.code == 413:
        return ServerValidationError("image is too large")
    if error.code == 429:
        retry = _safe_server_value(error.headers.get("Retry-After"), sensitive, 80)
        return ThrottleError("request throttled" + (f"; retry after {retry}" if retry else ""))
    if 300 <= error.code < 400:
        return NetworkError("server redirect rejected; retry with the configured server")
    if error.code >= 500:
        request_id = _safe_server_value(error.headers.get("X-Request-ID"), sensitive, 80)
        return NetworkError("server error; retry" + (f" (request id {request_id})" if request_id else ""))
    return SignerAuthError("authentication failed")


def _request(server, path, payload, bearer=None):
    body = json.dumps(payload, separators=(",", ":")).encode()
    if len(body) > 65_536:
        raise UsageConfigError("request is too large")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    request = Request(server + path, data=body, headers=headers, method="POST")
    sensitive = _sensitive_values(payload, bearer, (*headers.values(), server, path, body.decode()))
    try:
        response = OPENER.open(request, timeout=TIMEOUT)
    except HTTPError as error:
        raise _http_error(error, sensitive, "server rejected the tweet" if bearer else "server rejected the sign-in request")
    except (URLError, TimeoutError, OSError) as error:
        raise NetworkError("network or TLS error; retry") from error
    return _json_response(response)


def _required(data, name, typ):
    value = data.get(name)
    if not isinstance(value, typ) or (isinstance(value, str) and not value):
        raise NetworkError("server returned an invalid response")
    return value


def _validate_response_fields(data, fields):
    if set(data) != fields:
        raise NetworkError("server returned an invalid response")


def _challenge(data):
    _validate_response_fields(data, {"challenge_id", "message", "namespace", "expires_at"})
    challenge_id = _required(data, "challenge_id", str)
    try:
        canonical_id = str(uuid.UUID(challenge_id))
    except ValueError:
        raise NetworkError("server returned an invalid response")
    if len(challenge_id) != 36 or canonical_id != challenge_id:
        raise NetworkError("server returned an invalid response")
    message = _required(data, "message", str)
    try:
        encoded_message = message.encode("utf-8")
    except UnicodeEncodeError:
        raise NetworkError("server returned an invalid response")
    if len(encoded_message) > 4096:
        raise NetworkError("server returned an invalid response")
    namespace = data.get("namespace")
    if not isinstance(namespace, str) or len(namespace) > len(NAMESPACE) or namespace != NAMESPACE:
        raise NetworkError("server returned an invalid response")
    expires_at = _required(data, "expires_at", str)
    if len(expires_at) > 40 or not fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", expires_at):
        raise NetworkError("server returned an invalid response")
    try:
        datetime.fromisoformat(expires_at)
    except ValueError:
        raise NetworkError("server returned an invalid response")
    return challenge_id, encoded_message


def _token(data):
    _validate_response_fields(data, {"access_token", "token_type", "expires_in", "scopes"})
    token = _required(data, "access_token", str)
    scopes = data.get("scopes")
    if (len(token) > 4096 or any(ord(char) < 33 or ord(char) > 126 for char in token)
            or data.get("token_type") != "Bearer" or data.get("expires_in") != 600
            or not isinstance(scopes, list) or len(scopes) > 16
            or any(not isinstance(scope, str) or len(scope) > 128 for scope in scopes)
            or len(scopes) != len(set(scopes)) or set(scopes) != {"timeline:create", "timeline:read"}):
        raise NetworkError("server returned an invalid response")
    return token


def _image_kind(data):
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        brands = data[8:32]
        if any(brand in brands for brand in (b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1")):
            return "heic"
    return None


def _image_file(path):
    try:
        file_stat = path.lstat()
    except OSError:
        raise UsageConfigError("not a JPEG, PNG, WebP, or HEIC image") from None
    if not stat.S_ISREG(file_stat.st_mode):
        raise UsageConfigError("not a JPEG, PNG, WebP, or HEIC image")
    if file_stat.st_size > MAX_IMAGE_BYTES:
        raise UsageConfigError("image is too large (max 20 MB)")
    try:
        data = path.read_bytes()
    except OSError:
        raise UsageConfigError("not a JPEG, PNG, WebP, or HEIC image") from None
    if len(data) > MAX_IMAGE_BYTES:
        raise UsageConfigError("image is too large (max 20 MB)")
    kind = _image_kind(data)
    if kind == "heic":
        if sys.platform != "darwin":
            raise UsageConfigError("HEIC is not supported here; convert to JPEG first")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "converted.jpg"
            try:
                result = subprocess.run(
                    ["sips", "-s", "format", "jpeg", str(path), "--out", str(output)],
                    capture_output=True, check=False, shell=False,
                )
            except OSError:
                raise UsageConfigError("HEIC is not supported here; convert to JPEG first") from None
            if result.returncode:
                raise UsageConfigError("HEIC is not supported here; convert to JPEG first")
            try:
                data = output.read_bytes()
            except OSError:
                raise UsageConfigError("HEIC is not supported here; convert to JPEG first") from None
            if len(data) > MAX_IMAGE_BYTES:
                raise UsageConfigError("image is too large (max 20 MB)")
            if _image_kind(data) != "jpeg":
                raise UsageConfigError("HEIC is not supported here; convert to JPEG first")
    elif kind is None:
        raise UsageConfigError("not a JPEG, PNG, WebP, or HEIC image")
    return data


def _upload(server, data, bearer):
    request = Request(server + "/api/admin/media", data=data, headers={
        "Content-Type": "application/octet-stream",
        "Content-Length": str(len(data)),
        "Authorization": "Bearer " + bearer,
        "Accept": "application/json",
    }, method="POST")
    try:
        response = OPENER.open(request, timeout=UPLOAD_TIMEOUT)
    except HTTPError as error:
        raise _http_error(error, {bearer, server}, "server rejected the image")
    except (URLError, TimeoutError, OSError) as error:
        raise NetworkError("network or TLS error; retry") from error
    result = _json_response(response)
    _validate_response_fields(result, {"id", "width", "height"})
    image_id = _required(result, "id", str)
    try:
        if str(uuid.UUID(image_id)) != image_id:
            raise ValueError
    except ValueError:
        raise NetworkError("server returned an invalid response") from None
    for field in ("width", "height"):
        value = result.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise NetworkError("server returned an invalid response")
    return image_id


def post(server, key, text, attachments=(), alts=()):
    if not text:
        raise UsageConfigError("text must not be empty")
    if len(attachments) > 4:
        raise UsageConfigError("at most 4 images")
    if alts and len(alts) != len(attachments):
        raise UsageConfigError("give one --alt per --attach, or none")
    if any(len(alt) > 1000 or "\0" in alt for alt in alts):
        raise UsageConfigError("invalid alt text")
    image_data = [_image_file(path) for path in attachments]
    fingerprint = _fingerprint(key)
    challenge = _request(server, "/api/cli/auth/challenge", {"key_fingerprint": fingerprint})
    challenge_id, message = _challenge(challenge)
    signed = _run(["ssh-keygen", "-Y", "sign", "-n", NAMESPACE, "-f", str(key)], message)
    signature = _signature(signed)
    token_data = _request(server, "/api/cli/auth/token", {"challenge_id": challenge_id, "signature": signature})
    token = _token(token_data)
    payload = {"body_markdown": text}
    if attachments:
        payload["attachments"] = [{"id": _upload(server, data, token), "alt": alt}
                                   for data, alt in zip(image_data, alts or ("",) * len(attachments))]
    created = _request(server, "/api/admin/timeline", payload, token)
    entry_id = created.get("id")
    if not isinstance(entry_id, int) or isinstance(entry_id, bool) or entry_id < 1:
        raise NetworkError("server returned an invalid response")
    return f"{server}/timeline#entry-{entry_id}"


def parser():
    p = CliParser(prog="tweet")
    p.add_argument("--server")
    p.add_argument("--key")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--attach", action="append", type=Path)
    p.add_argument("--alt", action="append", default=[])
    p.add_argument("text")
    return p


def main(argv=None):
    try:
        args = parser().parse_args(argv)
        cfg = _config()
        server = _server(args.server or os.environ.get("WEEN_SERVER") or cfg.get("server", "https://www.neenerweener.ca"))
        key = _key(args.key or os.environ.get("WEEN_SSH_KEY") or cfg.get("key", "~/.ssh/ween_tweet_ed25519"))
        if args.attach or args.alt:
            result = post(server, key, args.text, args.attach or (), args.alt)
        else:
            result = post(server, key, args.text)
        print(result)
        return 0
    except tuple(ERROR_CODES) as error:
        print(f"tweet: {error}", file=sys.stderr)
        return ERROR_CODES[type(error)]
    except KeyboardInterrupt:
        print("tweet: interrupted", file=sys.stderr)
        return 130
    except Exception:  # noqa: BLE001 - final public safety boundary
        print("tweet: internal error", file=sys.stderr)
        return 1
