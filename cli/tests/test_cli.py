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

    def test_tweet_rejects_non_https_non_loopback_server(self):
        with self.assertRaises(cli.CliError): cli._server("http://example.com")
        self.assertEqual(cli._server("http://localhost:8000/"), "http://localhost:8000")

    def test_tweet_signs_server_message_with_exact_namespace_and_posts_bearer_request(self):
        calls=[]
        responses = iter([Response({"challenge_id":"c", "message":"line\nfinal\n"}), Response({"access_token":"tok-123"}), Response({"id":7})])
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

    def test_tweet_errors_never_include_token_signature_or_body(self):
        with patch.object(cli.OPENER, "open", side_effect=cli.HTTPError("https://s", 422, "x", {}, None)):
            with self.assertRaises(cli.CliError) as cm: cli._request("https://s", "/api/admin/timeline", {"body_markdown":"UNPUBLISHED_TEXT"}, "tok-123")
            self.assertNotIn("UNPUBLISHED_TEXT", str(cm.exception))

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
