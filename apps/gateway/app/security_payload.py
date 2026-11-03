"""Ephemeral encrypted storage for SECURITY-stream MIME payloads.

Identity-security mail can contain password-reset links or one-time verification
codes. Klyrow never stores SECURITY raw MIME as plaintext/base64. The root-owned
secret file is a small keyring: the first Fernet key encrypts new payloads and
subsequent keys remain decrypt-only during rotation. This supports zero-downtime
rotation while the worker purges ciphertext after delivery/dead-letter/expiry.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

LEGACY_PAYLOAD_VERSION = "fernet-v1"
PAYLOAD_VERSION = "fernet-keyring-v2"
DEFAULT_MAX_AGE_SECONDS = 3600


class SecurityPayloadError(ValueError):
    """Raised when a SECURITY payload cannot be safely encrypted/decrypted."""


def _key_path() -> Path:
    raw = os.getenv("KLYROW_SECURITY_PAYLOAD_KEY_FILE", "").strip()
    if not raw:
        raise SecurityPayloadError("KLYROW_SECURITY_PAYLOAD_KEY_FILE is required")
    return Path(raw)


def _key_id(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:16]


def _keyring() -> list[tuple[str, Fernet]]:
    """Load active + decrypt-only keys from the root-owned secret file.

    One Fernet key per non-empty line. The first key is active for new messages;
    older keys must remain until all ciphertext created with them has left the
    bounded retry/retention window. The key bytes themselves are never returned
    in payload metadata or logs.
    """

    try:
        lines = [line.strip() for line in _key_path().read_bytes().splitlines() if line.strip()]
    except OSError as exc:
        raise SecurityPayloadError("SECURITY payload encryption key is unavailable") from exc
    if not lines:
        raise SecurityPayloadError("SECURITY payload encryption keyring is empty")
    values: list[tuple[str, Fernet]] = []
    seen: set[str] = set()
    for key in lines:
        try:
            fernet = Fernet(key)
        except (TypeError, ValueError) as exc:
            raise SecurityPayloadError("SECURITY payload encryption key is invalid") from exc
        identifier = _key_id(key)
        if identifier in seen:
            raise SecurityPayloadError("SECURITY payload keyring contains a duplicate key")
        seen.add(identifier)
        values.append((identifier, fernet))
    return values


def max_payload_age_seconds() -> int:
    raw = os.getenv(
        "KLYROW_SECURITY_PAYLOAD_MAX_AGE_SECONDS",
        str(DEFAULT_MAX_AGE_SECONDS),
    ).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise SecurityPayloadError(
            "KLYROW_SECURITY_PAYLOAD_MAX_AGE_SECONDS must be an integer"
        ) from exc
    if value < 300 or value > 86400:
        raise SecurityPayloadError(
            "KLYROW_SECURITY_PAYLOAD_MAX_AGE_SECONDS must be between 300 and 86400"
        )
    return value


def encrypted_security_payload(
    raw: bytes,
    *,
    raw_sha256: str,
    message_id: str,
    stream: str,
) -> dict[str, Any]:
    if stream.upper() != "SECURITY":
        raise SecurityPayloadError("encrypted SECURITY payload requires SECURITY stream")
    key_id, active = _keyring()[0]
    token = active.encrypt(raw).decode("ascii")
    return {
        "raw_sha256": raw_sha256,
        "encrypted_raw": token,
        "encryption": PAYLOAD_VERSION,
        "key_id": key_id,
        "message_id": message_id,
        "size": len(raw),
        "stream": "SECURITY",
    }


def decrypt_security_payload(payload: dict[str, Any]) -> bytes:
    if payload.get("stream") != "SECURITY":
        raise SecurityPayloadError("unexpected security payload stream")
    version = payload.get("encryption")
    if version not in {PAYLOAD_VERSION, LEGACY_PAYLOAD_VERSION}:
        raise SecurityPayloadError("unsupported SECURITY payload encryption version")
    encoded = payload.get("encrypted_raw")
    if not isinstance(encoded, str) or not encoded:
        raise SecurityPayloadError("SECURITY payload ciphertext is missing")
    try:
        ciphertext = encoded.encode("ascii")
    except UnicodeEncodeError as exc:
        raise SecurityPayloadError("SECURITY payload ciphertext is invalid") from exc

    keyring = _keyring()
    if version == PAYLOAD_VERSION:
        requested = payload.get("key_id")
        if not isinstance(requested, str) or not requested:
            raise SecurityPayloadError("SECURITY payload key identifier is missing")
        for key_id, fernet in keyring:
            if key_id == requested:
                try:
                    return fernet.decrypt(ciphertext)
                except InvalidToken as exc:
                    raise SecurityPayloadError("SECURITY payload ciphertext is invalid") from exc
        raise SecurityPayloadError("SECURITY payload decryption key is unavailable")

    # Backward compatibility for fernet-v1 rows created before key IDs existed.
    # Try every retained key, but never reveal which keys failed.
    for _key_id_value, fernet in keyring:
        try:
            return fernet.decrypt(ciphertext)
        except InvalidToken:
            continue
    raise SecurityPayloadError("SECURITY payload ciphertext is invalid")


def scrubbed_security_payload(payload: dict[str, Any], *, reason: str) -> str:
    """Return privacy-safe JSON after the sensitive body has left its retry window."""

    safe = {
        "raw_sha256": str(payload.get("raw_sha256") or ""),
        "message_id": str(payload.get("message_id") or ""),
        "size": int(payload.get("size") or 0),
        "stream": "SECURITY",
        "body_state": "PURGED",
        "purge_reason": reason[:64],
    }
    return json.dumps(safe, separators=(",", ":"), sort_keys=True)
