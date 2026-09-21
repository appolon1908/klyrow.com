"""Dedicated durable-result key authority; no browser/session-key fallback."""
from __future__ import annotations

import base64
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

KEYRING_ENV = "KLYROW_DURABLE_RESULT_KEYRING_FILE"
MAX_KEYRING_BYTES = 8192
KEY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")


def _secure_open_flags(*extra: int) -> int:
    flags = os.O_RDONLY
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    nonblock = getattr(os, "O_NONBLOCK", 0)
    if not nofollow or not nonblock:
        raise KeyringError("durable_result_keyring_unavailable")
    flags |= nofollow | nonblock
    for flag in extra:
        flags |= flag
    return flags


class KeyringError(ValueError):
    """Deliberately carries no path, key material, or parser details."""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise KeyringError("durable_result_keyring_invalid")
        result[key] = value
    return result


@dataclass(frozen=True)
class Keyring:
    active_key_id: str
    keys: dict[str, bytes]


def parse_keyring(raw: bytes) -> Keyring:
    try:
        if not raw or len(raw) > MAX_KEYRING_BYTES:
            raise ValueError
        value = json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(value, dict) or set(value) != {"schema_version", "active_key_id", "keys"}:
            raise ValueError
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ValueError
        active, encoded = value["active_key_id"], value["keys"]
        if not isinstance(encoded, dict) or not 1 <= len(encoded) <= 8:
            raise ValueError
        keys = {}
        for key_id, material in encoded.items():
            if not KEY_ID.fullmatch(key_id) or not isinstance(material, str):
                raise ValueError
            key = base64.b64decode(material.encode("ascii"), altchars=b"-_", validate=True)
            if len(key) != 32:
                raise ValueError
            keys[key_id] = key
        if not isinstance(active, str) or active not in keys:
            raise ValueError
        return Keyring(active, keys)
    except (ValueError, TypeError, UnicodeError, KeyError, RecursionError):
        raise KeyringError("durable_result_keyring_invalid") from None


def load_keyring(path: str | Path | None = None) -> Keyring:
    selected = str(path) if path is not None else os.getenv(KEYRING_ENV, "")
    if not selected or not Path(selected).is_absolute():
        raise KeyringError("durable_result_keyring_required")
    descriptor = None
    try:
        descriptor = os.open(selected, _secure_open_flags())
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_KEYRING_BYTES:
            raise ValueError
        if metadata.st_mode & 0o022:
            raise ValueError
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            return parse_keyring(stream.read(MAX_KEYRING_BYTES + 1))
    except (OSError, ValueError, KeyringError):
        raise KeyringError("durable_result_keyring_unavailable") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def new_keyring_document() -> str:
    key_id = "dr-" + secrets.token_hex(8)
    return json.dumps({"schema_version": 1, "active_key_id": key_id,
                       "keys": {key_id: base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")}},
                      sort_keys=True) + "\n"


def keyring_ready() -> bool:
    """Safe configuration readback: no key ID, path, or material is disclosed."""
    try:
        load_keyring()
        return True
    except KeyringError:
        return False
