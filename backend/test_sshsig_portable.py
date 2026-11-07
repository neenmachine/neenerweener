import json
import subprocess
import uuid
from datetime import datetime, timezone

from machine_auth import (
    NAMESPACE,
    SUPPORTED_SCOPES,
    _message,
    _parse_keys,
    _verify_signature,
)


def _sign(key_path, message_path, namespace):
    signature_path = message_path.with_suffix(message_path.suffix + ".sig")
    signature_path.unlink(missing_ok=True)
    subprocess.run(
        ["ssh-keygen", "-Y", "sign", "-f", key_path, "-n", namespace, message_path],
        check=True,
        capture_output=True,
        timeout=10,
    )
    return signature_path.read_text()


def test_system_sshsig_verifies_exact_namespace_and_message(tmp_path):
    key_path = tmp_path / "key"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", key_path],
        check=True,
        capture_output=True,
        timeout=10,
    )
    public_key = " ".join(key_path.with_suffix(".pub").read_text().split()[:2])
    config = json.dumps(
        [
            {
                "name": "portable-test",
                "public_key": public_key,
                "scopes": sorted(SUPPORTED_SCOPES),
            }
        ]
    )
    key = next(iter(_parse_keys(config).values()))
    message = _message(
        uuid.UUID("12345678-1234-5678-1234-567812345678"),
        key.fingerprint,
        b"n" * 32,
        "https://example.test",
        datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
    ).encode()
    message_path = tmp_path / "message"
    message_path.write_bytes(message)

    signature = _sign(key_path, message_path, NAMESPACE)
    assert _verify_signature(message, signature, key)
    assert not _verify_signature(message + b"changed", signature, key)

    wrong_namespace_signature = _sign(key_path, message_path, "wrong-namespace")
    assert not _verify_signature(message, wrong_namespace_signature, key)
