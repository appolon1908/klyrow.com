"""Private keyring snapshots and read-only durable-record restore verification."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from .durable_keys import KEYRING_ENV, MAX_KEYRING_BYTES, load_keyring, parse_keyring

ARCHIVE_KEYRING = "durable-result-keyring.json"


class DurableBackupError(ValueError):
    """Callers must not expose paths, record contents or underlying exceptions."""


def _read_file(path: Path, limit: int, *, private: bool = True) -> bytes:
    if not path.is_absolute():
        raise DurableBackupError("invalid_backup_authority")
    flags = os.O_RDONLY
    if os.name != "nt":
        flags |= os.O_NOFOLLOW | os.O_NONBLOCK
    elif os.path.islink(path):
        raise DurableBackupError("invalid_backup_authority")
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        forbidden = 0o077 if private else 0o022
        owner_ok = os.name == "nt" or not hasattr(os, "geteuid") or before.st_uid == os.geteuid()
        if (not stat.S_ISREG(before.st_mode) or not owner_ok
                or (os.name != "nt" and before.st_mode & forbidden)
                or not 0 < before.st_size <= limit):
            raise DurableBackupError("invalid_backup_authority")
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if len(raw) > limit or any(getattr(before, f) != getattr(after, f) for f in fields):
            raise DurableBackupError("backup_authority_changed")
        return raw


def configured_keyring(env_file: Path) -> Path:
    """Read the generated literal assignment; never source or evaluate .env."""
    values = []
    for line in _read_file(env_file, 1048576, private=False).decode("utf-8").splitlines():
        name, separator, value = line.partition("=")
        if separator and name == KEYRING_ENV:
            values.append(value.strip())
    if len(values) != 1:
        raise DurableBackupError("missing_or_ambiguous_keyring_authority")
    value = values[0]
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    if not value or any(character in value for character in "$`\x00\r\n"):
        raise DurableBackupError("nonliteral_keyring_authority")
    path = Path(value)
    if not path.is_absolute():
        raise DurableBackupError("nonabsolute_keyring_authority")
    return path


def _stage(stage: Path) -> Path:
    if not stage.is_absolute():
        raise DurableBackupError("invalid_private_stage")
    info = stage.lstat()
    owner_ok = os.name == "nt" or not hasattr(os, "geteuid") or info.st_uid == os.geteuid()
    if not stat.S_ISDIR(info.st_mode) or not owner_ok or (os.name != "nt" and info.st_mode & 0o077):
        raise DurableBackupError("invalid_private_stage")
    return stage


def _key_bytes(path: Path) -> bytes:
    raw = _read_file(path, MAX_KEYRING_BYTES)
    parse_keyring(raw)
    return raw


def capture_keyring(env_file: Path, stage: Path) -> None:
    """Snapshot complete active/previous key authority, never regenerate it."""
    stage = _stage(stage)
    raw = _key_bytes(configured_keyring(env_file))
    fd, name = tempfile.mkstemp(prefix=".durable-backup-", dir=stage)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        # Refuse replacement even if another process created the destination.
        os.link(temporary, stage / ARCHIVE_KEYRING)
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        if os.name != "nt":
            directory_flags |= getattr(os, "O_NOFOLLOW", 0)
        directory = os.open(stage, directory_flags)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def check_source_unchanged(env_file: Path, stage: Path) -> None:
    stage = _stage(stage)
    if not hmac.compare_digest(_key_bytes(configured_keyring(env_file)), _key_bytes(stage / ARCHIVE_KEYRING)):
        raise DurableBackupError("backup_keyring_changed")


def check_archive(stage: Path) -> bytes:
    """Require a private keyring and one matching checksum-manifest entry."""
    stage = _stage(stage)
    raw = _key_bytes(stage / ARCHIVE_KEYRING)
    manifest = _read_file(stage / "MANIFEST.sha256", 131072)
    entries = [match.group(1).decode("ascii") for line in manifest.splitlines()
               if (match := re.fullmatch(rb"([0-9a-f]{64})  durable-result-keyring\.json", line))]
    if len(entries) != 1 or not hmac.compare_digest(entries[0], hashlib.sha256(raw).hexdigest()):
        raise DurableBackupError("backup_keyring_checksum_mismatch")
    return raw


def check_restore_coverage(env_file: Path, stage: Path) -> None:
    """Never overwrite live keys; require every archived key with identical bytes."""
    archived = parse_keyring(check_archive(stage))
    current = parse_keyring(_key_bytes(configured_keyring(env_file)))
    if any(not hmac.compare_digest(current.keys.get(kid, b""), material)
           for kid, material in archived.keys.items()):
        raise DurableBackupError("restore_keyring_incomplete")


def verify_restored_records(session) -> dict:
    """Authenticate every stored record, including expired results, without writes.

    Run only in the isolated restore rehearsal. Legacy rows are counted separately
    and are NOT represented as authenticated ciphertext. No record IDs or data
    are returned. Streaming bounds memory, not the duration of a complete scan.
    """
    from sqlalchemy import select, text
    from .durable_results import FORMAT, integration_document, read_control_response
    from .main import Idempotency
    from .operations import IntegrationResult

    if session.new or session.dirty or session.deleted:
        raise DurableBackupError("restore_verification_requires_clean_session")
    authority = load_keyring()
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
    report = {"schema_version": 1, "control_records": 0, "integration_records": 0,
              "authenticated_encrypted_records": 0, "legacy_records": 0}
    for model, field, decoder, counter in (
        (Idempotency, "response_json", read_control_response, "control_records"),
        (IntegrationResult, "payload_json", integration_document, "integration_records"),
    ):
        query = select(model).order_by(model.id).execution_options(yield_per=250)
        for row in session.scalars(query):
            decoder(row)  # Never bypass authentication because a result expired.
            envelope = json.loads(getattr(row, field))
            kind = "authenticated_encrypted_records" if envelope.get("format") == FORMAT else "legacy_records"
            report[counter] += 1
            report[kind] += 1
    if load_keyring() != authority:
        raise DurableBackupError("restore_verification_keyring_changed")
    return report


def main() -> int:
    """Read-only container entry point; ASGI startup/workers are never invoked."""
    try:
        from .main import DB
        with DB() as session:
            try:
                report = verify_restored_records(session)
            finally:
                session.rollback()
        print(json.dumps(report, sort_keys=True))
        return 0
    except Exception:
        # SQL exception strings may contain URLs, keys or record data.
        print("DURABLE_RESULT_RESTORE=BLOCKED", file=__import__("sys").stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
