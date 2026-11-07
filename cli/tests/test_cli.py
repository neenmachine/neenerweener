import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ween_tweet import cli

class Response:
    def __init__(self, data, headers=None):
        self.data = json.dumps(data).encode() if isinstance(data, dict) else data
        self.headers = headers or {}
    def read(self, n=-1): return self.data

class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.key = Path(self.tmp.name) / "key.pub"
        self.key.write_text("ssh-ed25519 AAAA test\n")

    def tearDown(self): self.tmp.cleanup()

    def test_tweet_prefers_flags_over_env_over_config(self):
        with patch.dict(os.environ, {"WEEN_SERVER":"https://env.example", "WEEN_SSH_KEY":str(self.key)}), patch.object(cli, "_config", return_value={"server":"https://config.example", "key":"bad"}), patch.object(cli, "post", return_value="https://flag.example/timeline#entry-1") as post:
            self.assertEqual(cli.main(["--server", "https://flag.example", "--key", str(self.key), "text"]), 0)
            post.assert_called_once_with("https://flag.example", self.key, "text")

    def test_tweet_rejects_invalid_utf8_config_without_leaking_file_details(self):
        config = Path(self.tmp.name) / ".config" / "ween" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_bytes(b"server = \xff secret-config-content")
        stderr = io.StringIO()
        with patch.dict(os.environ, {"HOME": self.tmp.name}, clear=False), contextlib.redirect_stderr(stderr):
            result = cli.main(["text"])
        self.assertEqual(result, 2)
        self.assertEqual(stderr.getvalue(), "tweet: cannot read config\n")
        self.assertNotIn(str(config), stderr.getvalue())
        self.assertNotIn("secret-config-content", stderr.getvalue())

    def test_tweet_rejects_non_https_non_loopback_server(self):
        with self.assertRaises(cli.CliError): cli._server("http://example.com")
        self.assertEqual(cli._server("http://localhost:8000/"), "http://localhost:8000")

    def test_tweet_signs_server_message_with_exact_namespace_and_posts_bearer_request(self):
        calls=[]
        responses = iter([Response({"challenge_id":"123e4567-e89b-12d3-a456-426614174000", "message":"line\nfinal\n", "namespace":cli.NAMESPACE, "expires_at":"2099-01-01T00:00:00Z"}), Response({"access_token":"tok-123", "token_type":"Bearer", "expires_in":600, "scopes":["timeline:write"]}), Response({"id":7})])
        def open_(request, timeout): calls.append(request); return next(responses)
        def run(args, stdin=None):
            if args[1:3] == ["-lf", str(self.key)]: return subprocess.CompletedProcess(args, 0, b"256 SHA256:fp comment\n", b"")
            self.assertEqual(args, ["ssh-keygen", "-Y", "sign", "-n", cli.NAMESPACE, "-f", str(self.key)])
            self.assertEqual(stdin, b"line\nfinal\n")
            return subprocess.CompletedProcess(args, 0, b"-----BEGIN SSH SIGNATURE-----\nabc\n", b"")
        with patch.object(cli.OPENER, "open", side_effect=open_), patch.object(cli, "_run", side_effect=run):
            self.assertEqual(cli.post("https://s.example", self.key, "hello"), "https://s.example/timeline#entry-7")
        self.assertEqual(json.loads(calls[0].data), {"key_fingerprint":"SHA256:fp"})
        self.assertEqual(calls[0].full_url, "https://s.example/api/cli/auth/challenge")
        self.assertEqual(calls[2].get_header("Authorization"), "Bearer tok-123")
        self.assertEqual(json.loads(calls[2].data), {"body_markdown":"hello"})

    def test_tweet_does_not_retry_ambiguous_create_failure(self):
        calls=[]
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
                        with patch.object(cli.OPENER, "open", side_effect=error):
                            with self.assertRaises(cli.CliError) as cm:
                                cli._request("https://s", "/api/admin/timeline", payload, bearer)
                        self.assertEqual(str(cm.exception), expected)

            error = cli.HTTPError("https://s", 422, "x", {}, None)
            error.read = lambda n=-1, secrets=secrets: json.dumps({"detail": "leak " + " ".join(secrets)}).encode()
            with patch.object(cli.OPENER, "open", side_effect=error):
                with self.assertRaises(cli.CliError) as cm:
                    cli._request("https://s", "/api/admin/timeline", payload, bearer)
            self.assertEqual(str(cm.exception), "server rejected the tweet")

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
                    with patch.object(cli.OPENER, "open", side_effect=error):
                        with self.assertRaises(cli.CliError) as cm:
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
        token = Response({"access_token":"token", "token_type":"Bearer", "expires_in":600, "scopes":["timeline:write"], "extra":secret})
        stderr = io.StringIO()
        with patch.object(cli, "_fingerprint", return_value="SHA256:fp"), patch.object(cli.OPENER, "open", side_effect=[challenge, token]), patch.object(cli, "_run", return_value=subprocess.CompletedProcess([], 0, b"-----BEGIN SSH SIGNATURE-----", b"")), contextlib.redirect_stderr(stderr):
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
            {"access_token":"token", "token_type":"Basic", "expires_in":600, "scopes":["timeline:write"]},
            {"access_token":"token", "token_type":"Bearer", "expires_in":601, "scopes":["timeline:write"]},
            {"access_token":"token", "token_type":"Bearer", "expires_in":600, "scopes":["timeline:write", "admin"]},
        ]:
            with self.assertRaisesRegex(cli.CliError, "invalid response"):
                cli._token(token)

    @unittest.skipUnless(shutil.which("ssh-keygen"), "ssh-keygen unavailable")
    def test_real_interoperability_signature_verifies(self):
        private = Path(self.tmp.name) / "id_ed25519"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(private)], check=True)
        message = b"exact message\n"
        signed = subprocess.run(["ssh-keygen", "-Y", "sign", "-n", cli.NAMESPACE, "-f", str(private)], input=message, stdout=subprocess.PIPE, check=True).stdout
        allowed = Path(self.tmp.name) / "allowed"
        allowed.write_text(f"test {private.with_suffix('.pub').read_text().strip()}\n")
        sig = Path(self.tmp.name) / "sig"; sig.write_bytes(signed)
        result = subprocess.run(["ssh-keygen", "-Y", "verify", "-n", cli.NAMESPACE, "-f", str(allowed), "-I", "test", "-s", str(sig)], input=message, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

if __name__ == "__main__": unittest.main()
