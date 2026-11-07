import base64
import contextlib
import io
import json
import os
import shutil
import socketserver
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

from ween_tweet import cli


class LocalServer(HTTPServer):
    # HTTPServer.server_bind calls socket.getfqdn (reverse DNS), which stalls on some macOS hosts.
    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class Response:
    def __init__(self, data, headers=None):
        self.data = json.dumps(data).encode() if isinstance(data, dict) else data
        self.headers = headers or {}
    def read(self, n=-1): return self.data


def signature_bytes():
    encoded = base64.b64encode(b"SSHSIG\0unit-test-signature").decode()
    return f"-----BEGIN SSH SIGNATURE-----\n{encoded}\n-----END SSH SIGNATURE-----\n".encode()


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.key = Path(self.tmp.name) / "key.pub"
        self.key.write_text("ssh-ed25519 AAAA test\n")
        self.key.chmod(0o600)

    def tearDown(self): self.tmp.cleanup()

    def test_tweet_prefers_flags_over_env_over_config(self):
        with patch.dict(os.environ, {"WEEN_SERVER":"https://env.example", "WEEN_SSH_KEY":str(self.key)}), patch.object(cli, "_config", return_value={"server":"https://config.example", "key":"bad"}), patch.object(cli, "post", return_value="https://flag.example/timeline#entry-1") as post:
            self.assertEqual(cli.main(["--server", "https://flag.example", "--key", str(self.key), "text"]), 0)
            post.assert_called_once_with("https://flag.example", self.key, "text")

    def test_tweet_normalizes_config_failures_to_exact_safe_errors(self):
        config = Path(self.tmp.name) / ".config" / "ween" / "config.toml"
        config.parent.mkdir(parents=True)
        cases = [
            (b"server = \xff secret-config-content", None, "tweet: cannot read config\n"),
            (b"server = [broken", None, "tweet: cannot read config\n"),
            (b"unknown = 'private-value'", None, "tweet: config contains an unknown key\n"),
            (b"server = 42", None, "tweet: config values must be strings\n"),
            (b"server = 'https://ok.example'", OSError("private-path-detail"), "tweet: cannot read config\n"),
        ]
        for content, read_error, expected in cases:
            with self.subTest(content=content[:20], read_error=bool(read_error)):
                config.write_bytes(content)
                stderr = io.StringIO()
                open_patch = patch.object(Path, "open", side_effect=read_error) if read_error else contextlib.nullcontext()
                with patch.dict(os.environ, {"HOME": self.tmp.name}, clear=False), open_patch, contextlib.redirect_stderr(stderr):
                    result = cli.main(["text"])
                self.assertEqual(result, 2)
                self.assertEqual(stderr.getvalue(), expected)
                self.assertNotIn(str(config), stderr.getvalue())
                self.assertNotIn("private", stderr.getvalue())
                self.assertNotIn("secret-config-content", stderr.getvalue())

    def test_tweet_rejects_malformed_server_urls_and_ports_as_config(self):
        cases = [
            "https://example.com:abc",
            "https://example.com:-1",
            "https://example.com:0",
            "https://example.com:65536",
            "https://example.com:999999999999999999999999",
            "https://[::1",
            "https://example.com: 443",
            "https://example.com:not-a-port/private-input",
            "https://:443",
            "https://@example.com",
            "https://user@example.com",
            "https://:password@example.com",
            "https://user:@example.com",
        ]
        for server in cases:
            with self.subTest(server=server):
                stderr = io.StringIO()
                with patch.object(cli, "_config", return_value={}), contextlib.redirect_stderr(stderr):
                    result = cli.main(["--server", server, "--key", str(self.key), "private tweet input"])
                self.assertEqual(result, 2)
                self.assertEqual(stderr.getvalue(), "tweet: invalid server URL\n")
                self.assertNotIn(server, stderr.getvalue())
                self.assertNotIn("private tweet input", stderr.getvalue())

    def test_tweet_rejects_bad_key_files_before_network(self):
        directory = Path(self.tmp.name) / "key-dir"
        directory.mkdir()
        symlink = Path(self.tmp.name) / "key-link"
        symlink.symlink_to(self.key)
        group_readable = Path(self.tmp.name) / "key-group-readable"
        group_readable.write_text("ssh-ed25519 AAAA test\\n")
        group_readable.chmod(0o640)
        owner_unreadable = Path(self.tmp.name) / "key-owner-unreadable"
        owner_unreadable.write_text("ssh-ed25519 AAAA test\\n")
        owner_unreadable.chmod(0o000)
        cases = [
            Path(self.tmp.name) / "missing-key",
            directory,
            symlink,
            group_readable,
            owner_unreadable,
        ]
        for key in cases:
            with self.subTest(key=key.name), patch.object(cli, "post") as post:
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    result = cli.main(["--server", "https://s.example", "--key", str(key), "private tweet input"])
                self.assertEqual(result, 2)
                self.assertEqual(stderr.getvalue(), "tweet: key file must be a private regular owner-readable file\n"
                                 if key != cases[0] else "tweet: key file is not readable\n")
                post.assert_not_called()

    def test_tweet_rejects_non_https_non_loopback_server(self):
        with self.assertRaises(cli.CliError): cli._server("http://example.com")
        self.assertEqual(cli._server("http://localhost:8000/"), "http://localhost:8000")

    def test_tweet_maps_usage_error_at_public_boundary(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = cli.main([])
        self.assertEqual(result, 2)
        self.assertEqual(stderr.getvalue(), "tweet: the following arguments are required: text\n")

    def test_tweet_maps_http_error_classes_at_public_boundary(self):
        cases = [
            (401, {}, 3, "tweet: authentication failed\n"),
            (422, {}, 4, "tweet: server rejected the tweet\n"),
            (429, {}, 5, "tweet: request throttled\n"),
            (500, {}, 1, "tweet: server error; retry\n"),
        ]
        for status, headers, expected_code, expected_message in cases:
            with self.subTest(status=status):
                error = cli.HTTPError("https://s", status, "private reason", headers, None)
                error.read = lambda n=-1: b"{}"
                stderr = io.StringIO()
                with patch.object(cli, "_config", return_value={}), patch.object(cli, "post", side_effect=cli._http_error(error, set(), "server rejected the tweet")), contextlib.redirect_stderr(stderr):
                    result = cli.main(["--server", "https://s.example", "--key", str(self.key), "private tweet input"])
                self.assertEqual(result, expected_code)
                self.assertEqual(stderr.getvalue(), expected_message)
                self.assertNotIn("private", stderr.getvalue())

    def test_tweet_maps_fingerprint_failure_to_signer_exit(self):
        stderr = io.StringIO()
        completed = subprocess.CompletedProcess([], 1, b"private output", b"private path")
        with patch.object(cli, "_run", return_value=completed), contextlib.redirect_stderr(stderr):
            result = cli.main(["--server", "https://s.example", "--key", str(self.key), "private tweet input"])
        self.assertEqual(result, 3)
        self.assertEqual(stderr.getvalue(), "tweet: cannot read the SSH key fingerprint\n")
        self.assertNotIn("private", stderr.getvalue())
        self.assertNotIn(str(self.key), stderr.getvalue())

    def test_tweet_signs_server_message_with_exact_namespace_and_posts_bearer_request(self):
        calls=[]
        responses = iter([Response({"challenge_id":"123e4567-e89b-12d3-a456-426614174000", "message":"line\nfinal\n", "namespace":cli.NAMESPACE, "expires_at":"2099-01-01T00:00:00Z"}), Response({"access_token":"tok-123", "token_type":"Bearer", "expires_in":600, "scopes":["timeline:create", "timeline:read"]}), Response({"id":7})])
        def open_(request, timeout): calls.append(request); return next(responses)
        def run(args, stdin=None):
            if args[1:3] == ["-lf", str(self.key)]: return subprocess.CompletedProcess(args, 0, b"256 SHA256:fp comment\n", b"")
            self.assertEqual(args, ["ssh-keygen", "-Y", "sign", "-n", cli.NAMESPACE, "-f", str(self.key)])
            self.assertEqual(stdin, b"line\nfinal\n")
            return subprocess.CompletedProcess(args, 0, signature_bytes(), b"")
        with patch.object(cli.OPENER, "open", side_effect=open_), patch.object(cli, "_run", side_effect=run):
            self.assertEqual(cli.post("https://s.example", self.key, "hello"), "https://s.example/timeline#entry-7")
        self.assertEqual(json.loads(calls[0].data), {"key_fingerprint":"SHA256:fp"})
        self.assertEqual(calls[0].full_url, "https://s.example/api/cli/auth/challenge")
        self.assertEqual(calls[2].get_header("Authorization"), "Bearer tok-123")
        self.assertEqual(json.loads(calls[2].data), {"body_markdown":"hello"})

    def test_tweet_normalizes_signer_failures_to_exact_safe_error(self):
        challenge = {
            "challenge_id":"123e4567-e89b-12d3-a456-426614174000",
            "message":"message",
            "namespace":cli.NAMESPACE,
            "expires_at":"2099-01-01T00:00:00Z",
        }
        cases = [
            subprocess.CompletedProcess([], 1, b"private signer output", b"private path"),
            subprocess.CompletedProcess([], 0, b"\xffprivate signer output", b""),
            subprocess.CompletedProcess([], 0, b"-----BEGIN SSH SIGNATURE-----\nnot-base64!\n-----END SSH SIGNATURE-----\n", b""),
            subprocess.CompletedProcess([], 0, b"-----BEGIN SSH SIGNATURE-----\nU1NIT1Q=\n-----END SSH SIGNATURE-----\n", b""),
            subprocess.CompletedProcess([], 0, signature_bytes() + b"private trailing output", b""),
        ]
        for completed in cases:
            with self.subTest(stdout=completed.stdout[:30], returncode=completed.returncode):
                stderr = io.StringIO()
                with patch.object(cli, "_fingerprint", return_value="SHA256:fp"), patch.object(cli, "_request", return_value=challenge), patch.object(cli, "_run", return_value=completed), contextlib.redirect_stderr(stderr):
                    result = cli.main(["--server", "https://s.example", "--key", str(self.key), "private tweet input"])
                self.assertEqual(result, 3)
                self.assertEqual(stderr.getvalue(), "tweet: SSH signing failed; check that the key is loaded or its passphrase is available\n")
                self.assertNotIn("private", stderr.getvalue())
                self.assertNotIn(str(self.key), stderr.getvalue())

        for signer_error, message in [
            (FileNotFoundError("private path"), "ssh-keygen not found"),
            (OSError("private path"), "cannot run ssh-keygen"),
        ]:
            with self.subTest(error=type(signer_error).__name__):
                stderr = io.StringIO()
                with patch.object(cli, "_fingerprint", return_value="SHA256:fp"), patch.object(cli, "_request", return_value=challenge), patch("subprocess.run", side_effect=signer_error), contextlib.redirect_stderr(stderr):
                    result = cli.main(["--server", "https://s.example", "--key", str(self.key), "private tweet input"])
                self.assertEqual(result, 3)
                self.assertEqual(stderr.getvalue(), f"tweet: {message}\n")
                self.assertNotIn("private", stderr.getvalue())
                self.assertNotIn(str(self.key), stderr.getvalue())

    def test_tweet_unexpected_exception_has_fixed_internal_error(self):
        stderr = io.StringIO()
        detail = f"unexpected private-input SECRET-TOKEN {self.tmp.name}"
        with patch.object(cli, "_config", return_value={}), patch.object(cli, "post", side_effect=RuntimeError(detail)), contextlib.redirect_stderr(stderr):
            result = cli.main(["--server", "https://s.example", "--key", str(self.key), "private-input"])
        self.assertEqual(result, 1)
        self.assertEqual(stderr.getvalue(), "tweet: internal error\n")
        for secret in ["Traceback", "RuntimeError", "unexpected", "private-input", "SECRET-TOKEN", self.tmp.name]:
            self.assertNotIn(secret, stderr.getvalue())

    def test_tweet_does_not_retry_ambiguous_create_failure(self):
        with patch.object(cli.OPENER, "open", side_effect=cli.HTTPError("https://s", 500, "x", {"X-Request-ID":"r"}, None)) as op:
            with self.assertRaisesRegex(cli.CliError, "server error") as cm: cli._request("https://s", "/api/admin/timeline", {"body_markdown":"x"}, "tok")
            self.assertEqual(cm.exception.code, 1); self.assertEqual(op.call_count, 1)

    def test_tweet_errors_never_include_request_input(self):
        cases = [
            ({"key_fingerprint":"SHA256:fingerprint-secret"}, None),
            ({"challenge_id":"challenge-secret"}, None),
            ({"message":"challenge-message-secret"}, None),
            ({"signature":"-----BEGIN SSH SIGNATURE-----\nsignature-secret\n-----END SSH SIGNATURE-----"}, None),
            ({"body_markdown":"UNPUBLISHED_TEXT"}, None),
            ({"body_markdown":"text"}, "bearer-secret"),
        ]
        headers = [
            (429, "Retry-After", "request throttled"),
            (500, "X-Request-ID", "server error; retry"),
        ]
        for payload, bearer in cases:
            secrets = [*payload.values(), *([bearer] if bearer else [])]
            for status, header, expected in headers:
                for secret in secrets:
                    with self.subTest(field=next(iter(payload)), bearer=bool(bearer), header=header):
                        error = cli.HTTPError("https://s", status, "x", {header: f"prefix-{secret}-suffix"}, None)
                        with patch.object(cli.OPENER, "open", side_effect=error), self.assertRaises(cli.CliError) as cm:
                                cli._request("https://s", "/api/admin/timeline", payload, bearer)
                        self.assertEqual(str(cm.exception), expected)

            error = cli.HTTPError("https://s", 422, "x", {}, None)
            error.read = lambda n=-1, secrets=secrets: json.dumps({"detail": "leak " + " ".join(secrets)}).encode()
            with patch.object(cli.OPENER, "open", side_effect=error), self.assertRaises(cli.CliError) as cm:
                    cli._request("https://s", "/api/admin/timeline", payload, bearer)
            self.assertEqual(str(cm.exception), "server rejected the tweet" if bearer else "server rejected the sign-in request")

    def test_tweet_omits_unsafe_server_headers(self):
        cases = ["line\nbreak", "null\x00byte", "delete\x7fbyte", "x" * 81]
        headers = [
            (429, "Retry-After", "request throttled"),
            (500, "X-Request-ID", "server error; retry"),
        ]
        for status, header, expected in headers:
            for value in cases:
                with self.subTest(header=header, value_length=len(value)):
                    error = cli.HTTPError("https://s", status, "x", {header: value}, None)
                    with patch.object(cli.OPENER, "open", side_effect=error), self.assertRaises(cli.CliError) as cm:
                            cli._request("https://s", "/path", {"body_markdown":"text"})
                    self.assertEqual(str(cm.exception), expected)

    def test_tweet_rejects_unknown_challenge_field_with_safe_exit(self):
        secret = "challenge-extra-secret"
        response = Response({"challenge_id":"123e4567-e89b-12d3-a456-426614174000", "message":"m", "namespace":cli.NAMESPACE, "expires_at":"2099-01-01T00:00:00Z", "extra":secret})
        stderr = io.StringIO()
        with patch.object(cli, "_fingerprint", return_value="SHA256:fp"), patch.object(cli.OPENER, "open", return_value=response), patch.object(cli, "_run") as run, contextlib.redirect_stderr(stderr):
            result = cli.main(["--server", "https://s.example", "--key", str(self.key), "text"])
        self.assertEqual(result, 1)
        run.assert_not_called()
        self.assertNotIn(secret, stderr.getvalue())

    def test_tweet_rejects_unknown_token_field_with_safe_exit(self):
        secret = "token-extra-secret"
        challenge = Response({"challenge_id":"123e4567-e89b-12d3-a456-426614174000", "message":"m", "namespace":cli.NAMESPACE, "expires_at":"2099-01-01T00:00:00Z"})
        token = Response({"access_token":"token", "token_type":"Bearer", "expires_in":600, "scopes":["timeline:create", "timeline:read"], "extra":secret})
        stderr = io.StringIO()
        with patch.object(cli, "_fingerprint", return_value="SHA256:fp"), patch.object(cli.OPENER, "open", side_effect=[challenge, token]), patch.object(cli, "_run", return_value=subprocess.CompletedProcess([], 0, signature_bytes(), b"")), contextlib.redirect_stderr(stderr):
            result = cli.main(["--server", "https://s.example", "--key", str(self.key), "text"])
        self.assertEqual(result, 1)
        self.assertNotIn(secret, stderr.getvalue())

    def test_tweet_rejects_invalid_challenge_and_token_protocol(self):
        valid_challenge = {"challenge_id":"123e4567-e89b-12d3-a456-426614174000", "message":"m", "namespace":cli.NAMESPACE, "expires_at":"2099-01-01T00:00:00Z"}
        for challenge in [
            {**valid_challenge, "challenge_id":"not-a-uuid"},
            {**valid_challenge, "namespace":"wrong"},
            {**valid_challenge, "expires_at":"2099-01-01T00:00:00+00:00"},
            {**valid_challenge, "message":"x" * 4097},
        ]:
            with self.assertRaisesRegex(cli.CliError, "invalid response"):
                cli._challenge(challenge)
        for token in [
            {"access_token":"token", "token_type":"Basic", "expires_in":600, "scopes":["timeline:create", "timeline:read"]},
            {"access_token":"token", "token_type":"Bearer", "expires_in":601, "scopes":["timeline:create", "timeline:read"]},
            {"access_token":"token", "token_type":"Bearer", "expires_in":600, "scopes":["timeline:create", "admin"]},
        ]:
            with self.assertRaisesRegex(cli.CliError, "invalid response"):
                cli._token(token)

    def test_tweet_ssh_keygen_runs_without_timeout(self):
        challenge = {"challenge_id":"123e4567-e89b-12d3-a456-426614174000", "message":"m", "namespace":cli.NAMESPACE, "expires_at":"2099-01-01T00:00:00Z"}
        token = {"access_token":"tok", "token_type":"Bearer", "expires_in":600, "scopes":["timeline:create", "timeline:read"]}
        def run(args, **kwargs):
            if args[1] == "-lf":
                return subprocess.CompletedProcess(args, 0, b"256 SHA256:fp comment\n", b"")
            return subprocess.CompletedProcess(args, 0, signature_bytes(), b"")
        with patch.object(cli, "_request", side_effect=[challenge, token, {"id":1}]), patch("subprocess.run", side_effect=run) as run_mock:
            cli.post("https://s.example", self.key, "text")
        self.assertEqual([c.args[0][:3] for c in run_mock.call_args_list], [["ssh-keygen", "-lf", str(self.key)], ["ssh-keygen", "-Y", "sign"]])
        for call in run_mock.call_args_list:
            self.assertNotIn("timeout", call.kwargs)

    def test_tweet_ctrl_c_exits_130_without_traceback(self):
        stderr = io.StringIO()
        with patch.object(cli, "_config", return_value={}), patch.object(cli, "_fingerprint", return_value="SHA256:fp"), \
                patch.object(cli, "_request", return_value={"challenge_id":"123e4567-e89b-12d3-a456-426614174000", "message":"m", "namespace":cli.NAMESPACE, "expires_at":"2099-01-01T00:00:00Z"}), \
                patch("subprocess.run", side_effect=KeyboardInterrupt), contextlib.redirect_stderr(stderr):
            try:
                result = cli.main(["--server", "https://s.example", "--key", str(self.key), "text"])
            except KeyboardInterrupt:
                self.fail("KeyboardInterrupt escaped main()")
        self.assertEqual(result, 130)
        self.assertEqual(stderr.getvalue(), "tweet: interrupted\n")

    def test_tweet_422_message_names_the_failed_step(self):
        challenge = Response({"challenge_id":"123e4567-e89b-12d3-a456-426614174000", "message":"m", "namespace":cli.NAMESPACE, "expires_at":"2099-01-01T00:00:00Z"})
        token = Response({"access_token":"tok", "token_type":"Bearer", "expires_in":600, "scopes":["timeline:create", "timeline:read"]})
        signed = subprocess.CompletedProcess([], 0, signature_bytes(), b"")
        for step, expected in [(0, "sign-in request"), (1, "sign-in request"), (2, "tweet")]:
            with self.subTest(step=step):
                error = cli.HTTPError("https://s", 422, "x", {}, None)
                error.read = lambda n=-1: b"{}"
                stderr = io.StringIO()
                with patch.object(cli, "_config", return_value={}), patch.object(cli, "_fingerprint", return_value="SHA256:fp"), \
                        patch.object(cli, "_run", return_value=signed), patch.object(cli.OPENER, "open", side_effect=[challenge, token][:step] + [error]), \
                        contextlib.redirect_stderr(stderr):
                    result = cli.main(["--server", "https://s.example", "--key", str(self.key), "text"])
                self.assertEqual(result, 4)
                self.assertEqual(stderr.getvalue(), f"tweet: server rejected the {expected}\n")

    def test_tweet_refuses_redirects(self):
        followed = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(302); self.send_header("Location", "/elsewhere"); self.send_header("Content-Length", "0"); self.end_headers()
            def do_GET(self):
                followed.append(self.path)
                body = b'{"id": 1}'
                self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            do_PUT = do_POST
            def log_message(self, *args): pass
        server = LocalServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with self.assertRaises(cli.NetworkError) as cm:
                cli._request(f"http://127.0.0.1:{server.server_port}", "/api/cli/auth/challenge", {"key_fingerprint":"SHA256:fp"})
        finally:
            server.shutdown(); server.server_close(); thread.join(10)
        self.assertEqual(str(cm.exception), "server redirect rejected; retry with the configured server")
        self.assertEqual(followed, [])

    def run_attachment_cli(self, args, responses, authenticate=True):
        calls = []
        responses = iter(responses)

        def open_(request, timeout):
            calls.append((request, timeout))
            response = next(responses)
            if isinstance(response, BaseException):
                raise response
            return response

        stderr = io.StringIO()
        stdout = io.StringIO()
        fingerprint = patch.object(cli, "_fingerprint", return_value="SHA256:fp") if authenticate else \
            patch.object(cli, "_fingerprint", side_effect=AssertionError("authentication"))
        with patch.object(cli.OPENER, "open", side_effect=open_), fingerprint, \
                patch.object(cli, "_run", return_value=subprocess.CompletedProcess([], 0, signature_bytes(), b"")), \
                contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(stdout):
            result = cli.main(["--server", "https://s.example", "--key", str(self.key), *args])
        return result, stderr.getvalue(), stdout.getvalue(), calls

    @staticmethod
    def auth_responses():
        return [
            Response({"challenge_id":"123e4567-e89b-12d3-a456-426614174000", "message":"m", "namespace":cli.NAMESPACE, "expires_at":"2099-01-01T00:00:00Z"}),
            Response({"access_token":"tok", "token_type":"Bearer", "expires_in":600, "scopes":["timeline:create", "timeline:read"]}),
        ]

    def image(self, name, data=b"\xff\xd8\xfftest"):
        path = Path(self.tmp.name) / name
        path.write_bytes(data)
        return path

    def test_tweet_attach_exact_command_uploads_once_then_creates(self):
        image = self.image("picture0.jpg")
        image_id = "123e4567-e89b-12d3-a456-426614174000"
        responses = self.auth_responses() + [Response({"id":image_id, "width":10, "height":20}), Response({"id":7})]

        result, stderr, stdout, calls = self.run_attachment_cli(
            ["look at this picture i took", "--attach", str(image)], responses,
        )

        self.assertEqual(result, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(stdout, "https://s.example/timeline#entry-7\n")
        self.assertEqual([call[0].full_url for call in calls].count("https://s.example/api/admin/media"), 1)
        self.assertEqual(calls[2][0].data, b"\xff\xd8\xfftest")
        self.assertEqual(calls[2][1], 120)
        self.assertEqual(calls[3][1], 15)
        self.assertEqual(calls[3][0].data, b'{"body_markdown":"look at this picture i took","attachments":[{"id":"123e4567-e89b-12d3-a456-426614174000","alt":""}]}')

    def test_tweet_attach_pairs_two_alts_in_order_through_main(self):
        first = self.image("one.png", b"\x89PNG\r\n\x1a\ntest")
        second = self.image("two.webp", b"RIFFxxxxWEBPtest")
        responses = self.auth_responses() + [
            Response({"id":"123e4567-e89b-12d3-a456-426614174000", "width":1, "height":1}),
            Response({"id":"223e4567-e89b-12d3-a456-426614174000", "width":2, "height":2}),
            Response({"id":7}),
        ]

        result, stderr, _, calls = self.run_attachment_cli([
            "hello", "--attach", str(first), "--attach", str(second), "--alt", "first", "--alt", "second",
        ], responses)

        self.assertEqual((result, stderr), (0, ""))
        self.assertEqual(calls[-1][0].data, b'{"body_markdown":"hello","attachments":[{"id":"123e4567-e89b-12d3-a456-426614174000","alt":"first"},{"id":"223e4567-e89b-12d3-a456-426614174000","alt":"second"}]}')

    def test_tweet_attach_rejects_design_limits_before_network_through_main(self):
        image = self.image("image.jpg")
        bad = self.image("bad.jpg", b"not image")
        large = self.image("large.jpg", b"0" * (cli.MAX_IMAGE_BYTES + 1))
        cases = [
            (["hello", "--attach", str(image), "--attach", str(image), "--alt", "one"], "give one --alt per --attach, or none"),
            (["hello", *sum((["--attach", str(image)] for _ in range(5)), [])], "at most 4 images"),
            (["hello", "--attach", str(bad)], "not a JPEG, PNG, WebP, or HEIC image"),
            (["hello", "--attach", str(large)], "image is too large (max 20 MB)"),
        ]
        for args, message in cases:
            with self.subTest(message=message):
                result, stderr, _, calls = self.run_attachment_cli(args, [])
                self.assertEqual(result, 2)
                self.assertEqual(stderr, f"tweet: {message}\n")
                self.assertEqual(calls, [])

    def test_tweet_attach_rejects_non_regular_files_and_growth_before_network(self):
        image = self.image("target.jpg")
        symlink = Path(self.tmp.name) / "link.jpg"
        symlink.symlink_to(image)
        directory = Path(self.tmp.name) / "directory.jpg"
        directory.mkdir()
        for path in (symlink, directory):
            with self.subTest(path=path.name):
                result, stderr, _, calls = self.run_attachment_cli(["hello", "--attach", str(path)], [])
                self.assertEqual(result, 2)
                self.assertEqual(stderr, "tweet: not a JPEG, PNG, WebP, or HEIC image\n")
                self.assertEqual(calls, [])

        with patch.object(Path, "read_bytes", return_value=b"0" * (cli.MAX_IMAGE_BYTES + 1)):
            result, stderr, _, calls = self.run_attachment_cli(["hello", "--attach", str(image)], [])
        self.assertEqual(result, 2)
        self.assertEqual(stderr, "tweet: image is too large (max 20 MB)\n")
        self.assertEqual(calls, [])

    def test_tweet_heic_darwin_uses_argv_and_cleans_temp_dir_on_success_and_sips_failure(self):
        for brand in (b"heic", b"msf1"):
            with self.subTest(brand=brand):
                heic = self.image(f"photo-{brand.decode()}.heic", b"xxxxftyp" + brand + b"xxxx")
                temp_dirs = []

                def run(args, **kwargs):
                    self.assertIsInstance(args, list)
                    self.assertEqual(args[0], "sips")
                    self.assertIs(kwargs.get("shell"), False)
                    temp_dirs.append(Path(args[-1]).parent)
                    Path(args[-1]).write_bytes(b"\xff\xd8\xffconverted")
                    return subprocess.CompletedProcess(args, 0, b"", b"")

                with patch.object(cli.sys, "platform", "darwin"), patch.object(cli.subprocess, "run", side_effect=run):
                    result, stderr, _, _ = self.run_attachment_cli(
                        ["hello", "--attach", str(heic)],
                        self.auth_responses() + [Response({"id":"123e4567-e89b-12d3-a456-426614174000", "width":1, "height":1}), Response({"id":7})],
                    )
                self.assertEqual((result, stderr), (0, ""))
                self.assertTrue(temp_dirs)
                self.assertFalse(temp_dirs[0].exists())

        heic = self.image("failure.heic", b"xxxxftypheicxxxx")
        temp_dirs = []
        def fail(args, **kwargs):
            temp_dirs.append(Path(args[-1]).parent)
            return subprocess.CompletedProcess(args, 1, b"", b"private error")
        with patch.object(cli.sys, "platform", "darwin"), patch.object(cli.subprocess, "run", side_effect=fail):
            result, stderr, _, calls = self.run_attachment_cli(["hello", "--attach", str(heic)], [])
        self.assertEqual(result, 2)
        self.assertEqual(stderr, "tweet: HEIC is not supported here; convert to JPEG first\n")
        self.assertEqual(calls, [])
        self.assertTrue(temp_dirs)
        self.assertFalse(temp_dirs[0].exists())

    def test_tweet_heic_rejects_linux_before_network_through_main(self):
        heic = self.image("photo.heic", b"xxxxftypheicxxxx")
        with patch.object(cli.sys, "platform", "linux"):
            result, stderr, _, calls = self.run_attachment_cli(["hello", "--attach", str(heic)], [])
        self.assertEqual(result, 2)
        self.assertEqual(stderr, "tweet: HEIC is not supported here; convert to JPEG first\n")
        self.assertEqual(calls, [])

    def test_tweet_heic_rejects_oversized_conversion_before_network(self):
        heic = self.image("photo.heic", b"xxxxftypheicxxxx")
        def run(args, **kwargs):
            Path(args[-1]).write_bytes(b"\xff\xd8\xff" + b"0" * cli.MAX_IMAGE_BYTES)
            return subprocess.CompletedProcess(args, 0, b"", b"")
        with patch.object(cli.sys, "platform", "darwin"), patch.object(cli.subprocess, "run", side_effect=run):
            result, stderr, _, calls = self.run_attachment_cli(["hello", "--attach", str(heic)], [])
        self.assertEqual(result, 2)
        self.assertEqual(stderr, "tweet: image is too large (max 20 MB)\n")
        self.assertEqual(calls, [])

    def test_tweet_upload_failures_use_public_exit_without_create_request(self):
        image = self.image("image.jpg")
        http_error = cli.HTTPError("https://s.example/api/admin/media", 422, "private", {}, None)
        http_error.read = lambda n=-1: b"{}"
        cases = [
            (http_error, 4, "tweet: server rejected the image\n"),
            (cli.HTTPError("https://s.example/api/admin/media", 413, "private", {}, None), 4,
             "tweet: image is too large (max 20 MB)\n"),
            (cli.URLError("private"), 1, "tweet: network or TLS error; retry\n"),
        ]
        for error, exit_code, message in cases:
            with self.subTest(error=type(error).__name__):
                result, stderr, _, calls = self.run_attachment_cli(
                    ["hello", "--attach", str(image)], self.auth_responses() + [error],
                )
                self.assertEqual(result, exit_code)
                self.assertEqual(stderr, message)
                self.assertEqual([call[0].full_url for call in calls][-1], "https://s.example/api/admin/media")
                self.assertNotIn("https://s.example/api/admin/timeline", [call[0].full_url for call in calls])

    def test_tweet_invalid_upload_response_exits_one_through_main(self):
        image = self.image("image.jpg")
        cases = [
            {"id":"123e4567-e89b-12d3-a456-426614174000", "width":1, "height":1, "extra":1},
            {"id":"123E4567-e89b-12d3-a456-426614174000", "width":1, "height":1},
        ]
        for response in cases:
            with self.subTest(response=response):
                result, stderr, _, calls = self.run_attachment_cli(
                    ["hello", "--attach", str(image)], self.auth_responses() + [Response(response)],
                )
                self.assertEqual(result, 1)
                self.assertEqual(stderr, "tweet: server returned an invalid response\n")
                self.assertNotIn("https://s.example/api/admin/timeline", [call[0].full_url for call in calls])

    def test_tweet_text_only_request_bytes_match_base(self):
        result, stderr, _, calls = self.run_attachment_cli(
            ["hello"], self.auth_responses() + [Response({"id":7})],
        )
        self.assertEqual((result, stderr), (0, ""))
        self.assertEqual(calls[-1][0].data, b'{"body_markdown":"hello"}')

    def test_tweet_rejects_invalid_text_and_alt_before_authentication(self):
        image = self.image("image.jpg")
        cases = [
            (["", "--attach", str(image)], "text must not be empty"),
            (["   "], "text must not be empty"),
            (["\t\n"], "text must not be empty"),
            (["\u00a0"], "text must not be empty"),
            (["\u2003"], "text must not be empty"),
            (["x" * 10001], "text is too long (max 10000 characters)"),
            (["bad\0text"], "invalid text"),
            (["hello", "--attach", str(image), "--alt", "x" * 1001], "invalid alt text"),
            (["hello", "--attach", str(image), "--alt", "bad\0alt"], "invalid alt text"),
        ]
        for args, message in cases:
            with self.subTest(message=message):
                result, stderr, _, calls = self.run_attachment_cli(args, [], authenticate=False)
                self.assertEqual(result, 2)
                self.assertEqual(stderr, f"tweet: {message}\n")
                self.assertEqual(calls, [])

    def test_tweet_accepts_text_at_character_limit_through_main(self):
        result, stderr, _, calls = self.run_attachment_cli(
            ["x" * 10000], self.auth_responses() + [Response({"id":7})],
        )
        self.assertEqual((result, stderr), (0, ""))
        self.assertEqual(len(json.loads(calls[-1][0].data)["body_markdown"]), 10000)

    def test_tweet_key_path_expands_home_in_config_and_default(self):
        home = Path(self.tmp.name)
        (home / ".ssh").mkdir()
        config = home / ".config" / "ween" / "config.toml"
        config.parent.mkdir(parents=True)
        for key_name, config_text in [("custom_key", 'key = "~/.ssh/custom_key"\n'), ("ween_tweet_ed25519", "")]:
            with self.subTest(key=key_name):
                key = home / ".ssh" / key_name
                key.write_text("private key\n"); key.chmod(0o600)
                config.write_text(config_text)
                env = {k: v for k, v in os.environ.items() if k not in ("WEEN_SSH_KEY", "WEEN_SERVER")}
                with patch.dict(os.environ, {**env, "HOME": str(home)}, clear=True), patch.object(cli, "post", return_value="ok") as post:
                    self.assertEqual(cli.main(["text"]), 0)
                post.assert_called_once_with("https://www.neenerweener.ca", key, "text")

    @unittest.skipUnless(shutil.which("ssh-keygen"), "ssh-keygen unavailable")
    def test_real_interoperability_signature_verifies(self):
        private = Path(self.tmp.name) / "id_ed25519"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(private)], check=True)
        message = b"exact message\n"
        signed = subprocess.run(["ssh-keygen", "-Y", "sign", "-n", cli.NAMESPACE, "-f", str(private)], input=message, capture_output=True, check=True).stdout
        allowed = Path(self.tmp.name) / "allowed"
        allowed.write_text(f"test {private.with_suffix('.pub').read_text().strip()}\n")
        sig = Path(self.tmp.name) / "sig"; sig.write_bytes(signed)
        result = subprocess.run(["ssh-keygen", "-Y", "verify", "-n", cli.NAMESPACE, "-f", str(allowed), "-I", "test", "-s", str(sig)], input=message, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

if __name__ == "__main__": unittest.main()
