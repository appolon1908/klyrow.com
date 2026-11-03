"""Versioned, context-authenticated control replays and integration results.

Legacy JSON remains read-only compatible during an explicit rollout; every new
control replay/result is encrypted with a dedicated file-backed keyring.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import HTTPException

from .durable_keys import KeyringError, load_keyring

FORMAT = "klyrow-durable-result.v1"
MAX_RESULT_BYTES = 65536
MAX_ENVELOPE_BYTES = 131072
MAX_DOCUMENT_BYTES = MAX_RESULT_BYTES + 1024
_SENSITIVE = (
    "password",
    "secret",
    "token",
    "authorization",
    "cookie",
    "credential",
    "privatekey",
    "apikey",
    "accesskey",
    "signature",
    "session",
    "reseturl",
    "verificationurl",
)
_CONTENT = {
    "body",
    "raw",
    "mime",
    "html",
    "htmlbody",
    "textbody",
    "headers",
    "url",
    "uri",
    "email",
    "recipient",
    "recipients",
    "phone",
    "cardnumber",
    "cvv",
}


def _unavailable() -> HTTPException:
    return HTTPException(503, "durable_result_unavailable")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_result_field")
        result[key] = value
    return result


def _loads(raw):
    return json.loads(raw, object_pairs_hook=_unique_object)


def canonical(value: dict[str, Any], *, limit: int = MAX_RESULT_BYTES) -> bytes:
    if not isinstance(value, dict):
        raise ValueError("result_must_be_object")
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(raw) > limit:
        raise ValueError("result_too_large")
    return raw


def redact_result(value: dict[str, Any]) -> dict[str, Any]:
    canonical(value)

    def visit(item, depth=0):
        if depth > 12:
            raise ValueError("result_too_deep")
        if isinstance(item, dict):
            output = {}
            for name, child in item.items():
                normalized = re.sub(r"[^a-z0-9]", "", name.lower())
                output[name] = (
                    "[REDACTED]"
                    if any(word in normalized for word in (*_CONTENT, *_SENSITIVE))
                    else visit(child, depth + 1)
                )
            return output
        if isinstance(item, list):
            if len(item) > 1000:
                raise ValueError("result_list_too_large")
            return [visit(child, depth + 1) for child in item]
        return item

    return visit(value)


def _binding(kind: str, *parts: str) -> list[str]:
    if any(not isinstance(part, str) or not part for part in parts):
        raise _unavailable()
    return [kind, *parts]


def seal(value: dict[str, Any], binding: list[str]) -> str:
    try:
        keys = load_keyring()
        kid = keys.active_key_id
        nonce = secrets.token_bytes(12)
        aad = json.dumps([FORMAT, kid, *binding], separators=(",", ":")).encode()
        cipher = AESGCM(keys.keys[kid]).encrypt(
            nonce, canonical(value, limit=MAX_DOCUMENT_BYTES), aad
        )
        return json.dumps(
            {
                "format": FORMAT,
                "kid": kid,
                "nonce": base64.urlsafe_b64encode(nonce).decode(),
                "ciphertext": base64.urlsafe_b64encode(cipher).decode(),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    except (KeyringError, ValueError, TypeError, OverflowError, RecursionError):
        raise _unavailable() from None


def unseal(raw: str, binding: list[str]) -> dict[str, Any]:
    try:
        if not isinstance(raw, str) or len(raw.encode()) > MAX_ENVELOPE_BYTES:
            raise ValueError
        value = _loads(raw)
        if not isinstance(value, dict):
            raise ValueError
        if "format" not in value:
            if os.getenv("KLYROW_DURABLE_RESULT_LEGACY_READ_ENABLED", "true").lower() != "true":
                raise ValueError
            canonical(value)
            return value
        if value.get("format") != FORMAT or set(value) != {"format", "kid", "nonce", "ciphertext"}:
            raise ValueError
        keys = load_keyring()
        kid = value["kid"]
        nonce = base64.b64decode(value["nonce"], altchars=b"-_", validate=True)
        ciphertext = base64.b64decode(value["ciphertext"], altchars=b"-_", validate=True)
        if len(nonce) != 12:
            raise ValueError
        aad = json.dumps([FORMAT, kid, *binding], separators=(",", ":")).encode()
        result = _loads(AESGCM(keys.keys[kid]).decrypt(nonce, ciphertext, aad))
        canonical(result, limit=MAX_DOCUMENT_BYTES)
        return result
    except (
        KeyringError,
        KeyError,
        ValueError,
        TypeError,
        InvalidTag,
        UnicodeError,
        RecursionError,
    ):
        raise _unavailable() from None


def seal_control_response(value, *, tenant_id, storage_key, request_hash, resource_id):
    return seal(
        value, _binding("control-replay", tenant_id, storage_key, request_hash, resource_id)
    )


def read_control_response(row):
    return unseal(
        row.response_json,
        _binding("control-replay", row.tenant_id, row.key, row.request_hash, row.resource_id),
    )


def seal_integration_result(value, *, tenant_id, outbox_id, source, result_key):
    digest = hashlib.sha256(canonical(value)).hexdigest()
    document = {"schema_version": 1, "request_hash": digest, "result": redact_result(value)}
    return seal(document, _binding("integration-result", tenant_id, outbox_id, source, result_key))


def integration_record(row):
    """Authenticate a payload or a retention tombstone in the same context."""
    value = unseal(
        row.payload_json,
        _binding("integration-result", row.tenant_id, row.outbox_id, row.source, row.result_key),
    )
    envelope = _loads(row.payload_json)
    if envelope.get("format") != FORMAT:
        return {
            "schema_version": 1,
            "request_hash": hashlib.sha256(canonical(value)).hexdigest(),
            "result": redact_result(value),
        }
    version = value.get("schema_version")
    if type(version) is not int or version not in (1, 2):
        raise _unavailable()
    expected = (
        {"schema_version", "request_hash", "result"}
        if version == 1
        else {"schema_version", "request_hash", "purged_at", "policy_sha256"}
    )
    if (
        set(value) != expected
        or not isinstance(value.get("request_hash"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", value["request_hash"])
    ):
        raise _unavailable()
    if version == 1:
        value["result"] = redact_result(value["result"])
    else:
        try:
            stamp = value["purged_at"]
            if not isinstance(stamp, str) or len(stamp) > 40:
                raise ValueError
            parsed = datetime.fromisoformat(stamp)
            if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
                raise ValueError
            if not isinstance(value["policy_sha256"], str) or not re.fullmatch(
                r"[0-9a-f]{64}", value["policy_sha256"]
            ):
                raise ValueError
        except (ValueError, TypeError):
            raise _unavailable() from None
    return value


def integration_document(row):
    value = integration_record(row)
    return value.get("result", {}), value["request_hash"]


def seal_result_tombstone(row, *, current: datetime, policy_sha256: str) -> str:
    value = integration_record(row)
    if value["schema_version"] == 2:
        return row.payload_json
    if (
        not isinstance(current, datetime)
        or current.tzinfo is None
        or not isinstance(policy_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", policy_sha256)
    ):
        raise ValueError("invalid_retention_authority")
    return seal(
        {
            "schema_version": 2,
            "request_hash": value["request_hash"],
            "purged_at": current.astimezone(UTC).isoformat(),
            "policy_sha256": policy_sha256,
        },
        _binding("integration-result", row.tenant_id, row.outbox_id, row.source, row.result_key),
    )


def result_retention_seconds() -> int:
    retention = int(os.getenv("KLYROW_RESULT_RETENTION_SECONDS", "2592000"))
    if not 3600 <= retention <= 7776000:
        raise ValueError("invalid_result_retention")
    return retention


def result_matches(row, value):
    return secrets.compare_digest(
        integration_document(row)[1], hashlib.sha256(canonical(value)).hexdigest()
    )


def result_readback(row, *, current: datetime | None = None):
    """Do not silently present absent, malformed, or expired results as success."""
    metadata = {"schema_version": 1, "availability": "UNAVAILABLE", "expires_at": None}
    if row is None:
        return {}, metadata
    try:
        retention = result_retention_seconds()
        created = row.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        expires = created + timedelta(seconds=retention)
        metadata["expires_at"] = expires.isoformat()
        value = integration_record(row)
        if value["schema_version"] == 2:
            metadata.update(availability="PURGED", purged_at=value["purged_at"])
            return {}, metadata
        if expires <= (current or datetime.now(UTC)):
            metadata["availability"] = "EXPIRED"
            return {}, metadata
        metadata["availability"] = "AVAILABLE"
        return value["result"], metadata
    except (HTTPException, ValueError, TypeError, AttributeError, OverflowError, RecursionError):
        metadata["availability"] = "INVALID"
        return {}, metadata


def rewrap_integration_result(row):
    """Preserve the original semantic digest, including fields removed by redaction."""
    return seal(
        integration_record(row),
        _binding("integration-result", row.tenant_id, row.outbox_id, row.source, row.result_key),
    )
