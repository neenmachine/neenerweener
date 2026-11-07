"""The minimal v0 timeline client."""
import argparse
import ipaddress
import json
import os
import socket
import subprocess
import sys
import tomllib
import uuid
from datetime import datetime
from re import fullmatch
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlunparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import __version__

MAX_RESPONSE = 1_048_576
TIMEOUT = 15
NAMESPACE = "neenerweener.ca/cli-auth/v1"

class CliError(Exception):
    def __init__(self, message, code):
        super().__init__(message)
        self.code = code

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

OPENER = build_opener(NoRedirect)


def _server(value):
    try:
        p = urlparse(value)
    except ValueError:
        raise CliError("invalid server URL", 2)
    if p.scheme not in ("http", "https") or not p.netloc or p.username or p.password or p.query or p.fragment:
        raise CliError("server must be an HTTPS URL (or HTTP localhost)", 2)
    host = (p.hostname or "").lower().rstrip(".")
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if p.scheme != "https" and not loopback:
        raise CliError("server must be HTTPS (HTTP is allowed only for localhost)", 2)
    return urlunparse((p.scheme, p.netloc, p.path.rstrip("/"), "", "", ""))


def _config():
    path = Path("~/.config/ween/config.toml").expanduser()
    if not path.exists():
        return {}
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise CliError(f"cannot read config: {e}", 2)
    if not isinstance(data, dict) or set(data) - {"server", "key"}:
        raise CliError("config contains an unknown key", 2)
    if any(not isinstance(v, str) for v in data.values()):
        raise CliError("config values must be strings", 2)
    return data


def _key(path):
    p = Path(path).expanduser()
    try:
        st = p.stat()
    except OSError:
        raise CliError("key file is not readable", 2)
    if not p.is_file() or not (st.st_mode & 0o444):
        raise CliError("key file must be a regular readable file", 2)
    return p


def _run(args, stdin=None):
    try:
        return subprocess.run(args, input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=TIMEOUT, check=False, shell=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise CliError("ssh-keygen is unavailable or timed out", 3) from e


def _fingerprint(key):
    result = _run(["ssh-keygen", "-lf", str(key), "-E", "sha256"])
    if result.returncode:
        raise CliError("cannot read the SSH key fingerprint", 2)
    fields = result.stdout.decode("utf-8", "replace").split()
    if len(fields) < 2 or not fields[1].startswith("SHA256:"):
        raise CliError("cannot read the SSH key fingerprint", 2)
    return fields[1]


def _json_response(response):
    data = response.read(MAX_RESPONSE + 1)
    if len(data) > MAX_RESPONSE:
        raise CliError("server response is too large", 1)
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CliError("server returned invalid JSON", 1)
    if not isinstance(value, dict):
        raise CliError("server returned an invalid response", 1)
    return value


def _sensitive_values(payload, bearer):
    values = {value for value in payload.values() if isinstance(value, str) and value}
    if bearer:
        values.add(bearer)
    return values


def _safe_detail(value, sensitive):
    return (isinstance(value, str) and len(value) <= 120 and "\n" not in value
            and "\r" not in value and not any(secret in value for secret in sensitive))


def _request(server, path, payload, bearer=None):
    body = json.dumps(payload, separators=(",", ":")).encode()
    sensitive = _sensitive_values(payload, bearer)
    if len(body) > 65_536:
        raise CliError("request is too large", 2)
    url = server + path
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    req = Request(url, data=body, headers=headers, method="POST")
    try:
        response = OPENER.open(req, timeout=TIMEOUT)
    except HTTPError as e:
        # Redirects are rejected by NoRedirect. Read only a bounded body for safe functional errors.
        retry = e.headers.get("Retry-After")
        request_id = e.headers.get("X-Request-ID")
        if e.code == 422:
            detail = "server rejected the tweet"
            try:
                value = _json_response(e)
                candidate = value.get("detail")
                if _safe_detail(candidate, sensitive):
                    detail = candidate
            except CliError:
                pass
            raise CliError(detail, 4)
        if e.code == 429:
            suffix = _safe_header(retry)
            raise CliError("request throttled" + ((f"; retry after {suffix}" if suffix else "")), 5)
        if 300 <= e.code < 400:
            raise CliError("server redirect rejected; retry with the configured server", 1)
        if e.code >= 500:
            suffix = _safe_header(request_id)
            raise CliError("server error; retry" + ((f" (request id {suffix})" if suffix else "")), 1)
        raise CliError("authentication failed", 3)
    except (URLError, socket.timeout, TimeoutError, OSError) as e:
        raise CliError("network or TLS error; retry", 1) from e
    return _json_response(response)


def _safe_header(value):
    if not value or len(value) > 80 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        return None
    return value


def _required(data, name, typ):
    value = data.get(name)
    if not isinstance(value, typ) or (isinstance(value, str) and not value):
        raise CliError("server returned an invalid response", 1)
    return value


def _challenge(data):
    challenge_id = _required(data, "challenge_id", str)
    try:
        canonical_id = str(uuid.UUID(challenge_id))
    except ValueError:
        raise CliError("server returned an invalid response", 1)
    if len(challenge_id) != 36 or canonical_id != challenge_id:
        raise CliError("server returned an invalid response", 1)
    message = _required(data, "message", str)
    try:
        encoded_message = message.encode("utf-8")
    except UnicodeEncodeError:
        raise CliError("server returned an invalid response", 1)
    if len(encoded_message) > 4096:
        raise CliError("server returned an invalid response", 1)
    namespace = data.get("namespace")
    if not isinstance(namespace, str) or len(namespace) > len(NAMESPACE) or namespace != NAMESPACE:
        raise CliError("server returned an invalid response", 1)
    expires_at = _required(data, "expires_at", str)
    if len(expires_at) > 40 or not fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", expires_at):
        raise CliError("server returned an invalid response", 1)
    try:
        datetime.fromisoformat(expires_at[:-1] + "+00:00")
    except ValueError:
        raise CliError("server returned an invalid response", 1)
    return challenge_id, encoded_message


def _token(data):
    token = _required(data, "access_token", str)
    scopes = data.get("scopes")
    if (len(token) > 4096 or any(ord(char) < 33 or ord(char) > 126 for char in token)
            or data.get("token_type") != "Bearer" or data.get("expires_in") != 600
            or not isinstance(scopes, list) or len(scopes) > 16
            or any(not isinstance(scope, str) or len(scope) > 128 for scope in scopes)
            or len(scopes) != len(set(scopes)) or set(scopes) != {"timeline:write"}):
        raise CliError("server returned an invalid response", 1)
    return token


def post(server, key, text):
    fingerprint = _fingerprint(key)
    challenge = _request(server, "/api/cli/auth/challenge", {"key_fingerprint": fingerprint})
    challenge_id, message = _challenge(challenge)
    signed = _run(["ssh-keygen", "-Y", "sign", "-n", NAMESPACE, "-f", str(key)], message)
    if signed.returncode or not signed.stdout.startswith(b"-----BEGIN SSH SIGNATURE-----"):
        raise CliError("SSH signing failed; check that the key is loaded or its passphrase is available", 3)
    token_data = _request(server, "/api/cli/auth/token", {"challenge_id": challenge_id, "signature": signed.stdout.decode("ascii", "strict")})
    token = _token(token_data)
    created = _request(server, "/api/admin/timeline", {"body_markdown": text}, token)
    entry_id = created.get("id")
    if not isinstance(entry_id, int) or isinstance(entry_id, bool) or entry_id < 1:
        raise CliError("server returned an invalid response", 1)
    return f"{server}/timeline#entry-{entry_id}"


def parser():
    p = argparse.ArgumentParser(prog="tweet")
    p.add_argument("--server")
    p.add_argument("--key")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("text")
    return p


def main(argv=None):
    try:
        args = parser().parse_args(argv)
        cfg = _config()
        server = _server(args.server or os.environ.get("WEEN_SERVER") or cfg.get("server", "https://www.neenerweener.ca"))
        key = _key(args.key or os.environ.get("WEEN_SSH_KEY") or cfg.get("key", "~/.ssh/ween_tweet_ed25519.pub"))
        print(post(server, key, args.text))
        return 0
    except CliError as e:
        print(f"tweet: {e}", file=sys.stderr)
        return e.code
    except (KeyboardInterrupt, BrokenPipeError):
        return 1
