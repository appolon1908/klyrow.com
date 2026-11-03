import json
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from apps.gateway.app.security_payload import (
    LEGACY_PAYLOAD_VERSION,
    PAYLOAD_VERSION,
    SecurityPayloadError,
    decrypt_security_payload,
    encrypted_security_payload,
    max_payload_age_seconds,
    scrubbed_security_payload,
)


def configure_keys(tmp_path, monkeypatch, *keys: bytes):
    key_file = tmp_path / "security-payload-key"
    material = keys or (Fernet.generate_key(),)
    key_file.write_bytes(b"\n".join(material) + b"\n")
    monkeypatch.setenv("KLYROW_SECURITY_PAYLOAD_KEY_FILE", str(key_file))
    return key_file, material


def test_security_payload_is_encrypted_versioned_and_scrubbed(tmp_path, monkeypatch):
    configure_keys(tmp_path, monkeypatch)
    raw = b"Subject: Verify\r\n\r\nYour code is 482731"
    payload = encrypted_security_payload(
        raw,
        raw_sha256="abc123",
        message_id="<message@example.test>",
        stream="SECURITY",
    )

    serialized = json.dumps(payload)
    assert "482731" not in serialized
    assert "raw_b64" not in payload
    assert payload["encryption"] == PAYLOAD_VERSION
    assert len(payload["key_id"]) == 16
    assert decrypt_security_payload(payload) == raw

    scrubbed = json.loads(scrubbed_security_payload(payload, reason="provider_submitted"))
    assert scrubbed["body_state"] == "PURGED"
    assert scrubbed["purge_reason"] == "provider_submitted"
    assert "encrypted_raw" not in scrubbed
    assert "key_id" not in scrubbed
    assert "482731" not in json.dumps(scrubbed)


def test_key_rotation_keeps_old_ciphertext_decryptable(tmp_path, monkeypatch):
    old_key = Fernet.generate_key()
    new_key = Fernet.generate_key()
    configure_keys(tmp_path, monkeypatch, old_key)
    raw = b"Subject: Reset\r\n\r\nSensitive security link"
    old_payload = encrypted_security_payload(
        raw,
        raw_sha256="old-digest",
        message_id="old-message",
        stream="SECURITY",
    )

    configure_keys(tmp_path, monkeypatch, new_key, old_key)
    new_payload = encrypted_security_payload(
        raw,
        raw_sha256="new-digest",
        message_id="new-message",
        stream="SECURITY",
    )

    assert old_payload["key_id"] != new_payload["key_id"]
    assert decrypt_security_payload(old_payload) == raw
    assert decrypt_security_payload(new_payload) == raw


def test_legacy_fernet_v1_payload_survives_rotation_window(tmp_path, monkeypatch):
    old_key = Fernet.generate_key()
    new_key = Fernet.generate_key()
    configure_keys(tmp_path, monkeypatch, new_key, old_key)
    raw = b"legacy sensitive message"
    legacy = {
        "raw_sha256": "legacy",
        "encrypted_raw": Fernet(old_key).encrypt(raw).decode("ascii"),
        "encryption": LEGACY_PAYLOAD_VERSION,
        "message_id": "legacy-message",
        "size": len(raw),
        "stream": "SECURITY",
    }
    assert decrypt_security_payload(legacy) == raw


def test_removing_old_key_fails_closed_for_old_ciphertext(tmp_path, monkeypatch):
    old_key = Fernet.generate_key()
    new_key = Fernet.generate_key()
    configure_keys(tmp_path, monkeypatch, old_key)
    payload = encrypted_security_payload(
        b"secret",
        raw_sha256="digest",
        message_id="message",
        stream="SECURITY",
    )
    configure_keys(tmp_path, monkeypatch, new_key)
    with pytest.raises(SecurityPayloadError, match="decryption key is unavailable"):
        decrypt_security_payload(payload)


def test_security_payload_key_is_required(monkeypatch):
    monkeypatch.delenv("KLYROW_SECURITY_PAYLOAD_KEY_FILE", raising=False)
    with pytest.raises(SecurityPayloadError):
        encrypted_security_payload(
            b"secret",
            raw_sha256="digest",
            message_id="message",
            stream="SECURITY",
        )


def test_payload_retention_is_bounded(monkeypatch):
    monkeypatch.setenv("KLYROW_SECURITY_PAYLOAD_MAX_AGE_SECONDS", "3600")
    assert max_payload_age_seconds() == 3600
    monkeypatch.setenv("KLYROW_SECURITY_PAYLOAD_MAX_AGE_SECONDS", "86401")
    with pytest.raises(SecurityPayloadError):
        max_payload_age_seconds()


def test_standard_launchers_mount_security_payload_secret():
    root = Path(__file__).resolve().parents[1]
    override = "deploy/docker-compose.security-mail.yml"
    for relative in (
        "scripts/start",
        "scripts/update",
        "scripts/deploy",
    ):
        content = (root / relative).read_text(encoding="utf-8")
        assert override in content, f"{relative} must include SECURITY mail compose override"

    unit = (root / "config/systemd/klyrow-stack.service").read_text(encoding="utf-8")
    assert "ExecStart=/root/klyrow.com/scripts/start" in unit
    assert "ExecStop=/root/klyrow.com/scripts/stop" in unit
    assert "docker compose" not in unit


def test_security_retention_covers_sandbox_and_lease_terminal_paths():
    root = Path(__file__).resolve().parents[1]
    worker = (root / "apps/gateway/app/security_smtp_worker.py").read_text(encoding="utf-8")
    assert "SandboxCapture" in worker
    assert '"DELIVERED"' in worker
    assert '"DEAD_LETTER"' in worker
    assert "capture.content_json = safe_json" in worker
    assert "payload_retention_expired" in worker
