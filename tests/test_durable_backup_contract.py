"""Regression guards for encrypted-data readiness and recoverability."""

from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from apps.gateway.app.main import readiness_alias, readyz
from apps.gateway.app.durable_keys import KEYRING_ENV

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("endpoint", [readyz, readiness_alias])
def test_missing_result_key_cannot_report_ready(monkeypatch, endpoint):
    monkeypatch.delenv(KEYRING_ENV)
    with pytest.raises(HTTPException) as error:
        endpoint(Mock())
    assert error.value.status_code == 503
    assert error.value.detail == "durable_result_keys_unavailable"


def test_backup_captures_keyring_before_database_dump():
    script = (ROOT / "scripts/backup").read_text()
    assert script.index("durable-keyring-backup capture") < script.index("pg_dump")
    assert script.count("durable-result-keyring.json") >= 2
    assert "durable-keyring-backup check-source" in script


def test_restore_checks_key_coverage_before_overwriting_databases():
    script = (ROOT / "scripts/restore").read_text()
    assert script.index("durable-keyring-backup check-restore") < script.index("pg_restore")


def test_restore_rehearsal_authenticates_restored_ciphertext():
    script = (ROOT / "scripts/restore-verify").read_text()
    assert "KLYROW_DURABLE_RESULT_KEYRING_FILE=/run/secrets/durable-result-keyring.json" in script
    assert script.index("python -m app.durable_backup") < script.index(
        "printf 'SERVER37_RESTORE=PASS"
    )


# These are isolated synthetic files and databases, never production authority.
import base64
import hashlib
import json
import os
import runpy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from apps.gateway.app.main import Base, Idempotency, healthz
from apps.gateway.app import durable_backup as backup
from apps.gateway.app.durable_keys import KeyringError, new_keyring_document
from apps.gateway.app.durable_results import seal_control_response, seal_integration_result
from apps.gateway.app.operations import IntegrationResult


@pytest.fixture
def archive_fixture(tmp_path, isolated_durable_result_keyring):
    stage = tmp_path / "stage"
    stage.mkdir(mode=0o700)
    env = tmp_path / ".env"
    env.write_text(f"{KEYRING_ENV}={isolated_durable_result_keyring}\n")
    env.chmod(0o600)
    return SimpleNamespace(
        stage=stage,
        env=env,
        source=isolated_durable_result_keyring,
        target=stage / backup.ARCHIVE_KEYRING,
    )


def manifest(fixture):
    digest = hashlib.sha256(fixture.target.read_bytes()).hexdigest()
    path = fixture.stage / "MANIFEST.sha256"
    path.write_text(f"{digest}  {backup.ARCHIVE_KEYRING}\n")
    path.chmod(0o600)
    return path


def add_key(path, name="next"):
    data = json.loads(path.read_text())
    data["keys"][name] = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
    data["active_key_id"] = name
    path.write_text(json.dumps(data))
    return data


def test_capture_preserves_active_and_previous_keys_privately_without_overwrite(archive_fixture):
    f = archive_fixture
    add_key(f.source)
    original = f.source.read_bytes()
    backup.capture_keyring(f.env, f.stage)
    assert f.target.read_bytes() == original
    assert f.target.stat().st_mode & 0o777 == 0o600
    backup.check_source_unchanged(f.env, f.stage)
    with pytest.raises(FileExistsError):
        backup.capture_keyring(f.env, f.stage)
    assert f.target.read_bytes() == f.source.read_bytes() == original
    assert list(f.stage.glob(".durable-backup-*")) == []


@pytest.mark.parametrize("config", ["absent", "duplicate", "relative", "shell", "empty"])
def test_config_is_one_literal_absolute_authority(archive_fixture, config):
    f = archive_fixture
    f.env.write_text(
        {
            "absent": "KLYROW_ENV=test\n",
            "duplicate": f"{KEYRING_ENV}={f.source}\n{KEYRING_ENV}={f.source}\n",
            "relative": f"{KEYRING_ENV}=relative.json\n",
            "shell": f"{KEYRING_ENV}=$(touch should-never-exist)\n",
            "empty": f"{KEYRING_ENV}=\n",
        }[config]
    )
    with pytest.raises(backup.DurableBackupError):
        backup.capture_keyring(f.env, f.stage)
    assert not f.target.exists()


@pytest.mark.parametrize("quote", ["", "'", '"'])
def test_literal_quoted_path_and_environment_override(archive_fixture, monkeypatch, quote):
    f = archive_fixture
    monkeypatch.setenv(KEYRING_ENV, "/untrusted/environment/override")
    f.env.write_text(f"{KEYRING_ENV}={quote}{f.source}{quote}\n")
    backup.capture_keyring(f.env, f.stage)
    assert f.target.read_bytes() == f.source.read_bytes()


@pytest.mark.parametrize("kind", ["missing", "symlink", "fifo", "public", "oversized", "malformed"])
def test_keyring_invalid_file_types_modes_and_contents_rejected(archive_fixture, kind):
    f = archive_fixture
    original = f.source.read_bytes()
    f.source.unlink()
    if kind == "symlink":
        target = f.source.with_name("real.json")
        target.write_bytes(original)
        target.chmod(0o600)
        f.source.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(f.source, 0o600)
    elif kind != "missing":
        f.source.write_bytes(
            original if kind == "public" else b"x" * (9000 if kind == "oversized" else 1)
        )
        f.source.chmod(0o644 if kind == "public" else 0o600)
    with pytest.raises((OSError, ValueError, KeyringError)):
        backup.capture_keyring(f.env, f.stage)
    assert not f.target.exists()


@pytest.mark.parametrize("kind", ["public-stage", "symlink-stage", "symlink-env", "writable-env"])
def test_stage_and_config_boundaries(archive_fixture, kind):
    f = archive_fixture
    stage, env = f.stage, f.env
    if kind == "public-stage":
        stage.chmod(0o755)
    elif kind == "symlink-stage":
        stage = f.stage.with_name("stage-link")
        stage.symlink_to(f.stage, target_is_directory=True)
    elif kind == "symlink-env":
        env = f.env.with_name("env-link")
        env.symlink_to(f.env)
    else:
        env.chmod(0o666)
    with pytest.raises((OSError, ValueError)):
        backup.capture_keyring(env, stage)
    assert not f.target.exists()


def test_detects_key_rotation_during_backup(archive_fixture):
    f = archive_fixture
    backup.capture_keyring(f.env, f.stage)
    add_key(f.source)
    with pytest.raises(backup.DurableBackupError, match="backup_keyring_changed"):
        backup.check_source_unchanged(f.env, f.stage)


@pytest.mark.parametrize(
    "damage", ["missing-entry", "wrong-digest", "duplicate-entry", "changed-key", "missing-key"]
)
def test_archive_requires_matching_keyring_manifest(archive_fixture, damage):
    f = archive_fixture
    backup.capture_keyring(f.env, f.stage)
    sums = manifest(f)
    assert backup.check_archive(f.stage) == f.source.read_bytes()
    if damage == "missing-entry":
        sums.write_text("unrelated\n")
    elif damage == "wrong-digest":
        sums.write_text(f"{'0' * 64}  {backup.ARCHIVE_KEYRING}\n")
    elif damage == "duplicate-entry":
        sums.write_text(sums.read_text() * 2)
    elif damage == "changed-key":
        add_key(f.target)
    else:
        f.target.unlink()
    with pytest.raises((OSError, ValueError)):
        backup.check_archive(f.stage)


def test_restore_accepts_superset_but_never_replaces_live_authority(archive_fixture):
    f = archive_fixture
    backup.capture_keyring(f.env, f.stage)
    manifest(f)
    add_key(f.source)
    current = f.source.read_bytes()
    backup.check_restore_coverage(f.env, f.stage)
    assert f.source.read_bytes() == current
    assert f.target.read_bytes() != current


@pytest.mark.parametrize("damage", ["missing-id", "replaced-material"])
def test_restore_rejects_incomplete_live_authority_without_modification(archive_fixture, damage):
    f = archive_fixture
    original_id = json.loads(f.source.read_text())["active_key_id"]
    backup.capture_keyring(f.env, f.stage)
    manifest(f)
    current = add_key(f.source)
    if damage == "missing-id":
        del current["keys"][original_id]
    else:
        current["keys"][original_id] = base64.urlsafe_b64encode(os.urandom(32)).decode()
    f.source.write_text(json.dumps(current))
    before = f.source.read_bytes()
    with pytest.raises(backup.DurableBackupError, match="restore_keyring_incomplete"):
        backup.check_restore_coverage(f.env, f.stage)
    assert f.source.read_bytes() == before


def test_cli_errors_are_sanitized_and_root_is_required(archive_fixture, monkeypatch, capsys):
    module = runpy.run_path(str(ROOT / "scripts/durable-keyring-backup"))
    main = module["main"]
    monkeypatch.setitem(
        main.__globals__,
        "require_root",
        lambda: (_ for _ in ()).throw(PermissionError("secret-path")),
    )
    f = archive_fixture
    assert main(["capture", "--env-file", str(f.env), "--stage", str(f.stage)]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "DURABLE_KEYRING_BACKUP=BLOCKED\n"
    assert not f.target.exists()
    monkeypatch.setattr(os, "geteuid", lambda: 10001)
    with pytest.raises(PermissionError):
        module["require_root"]()


def test_valid_keys_are_required_for_both_ready_aliases_and_not_liveness(monkeypatch):
    for endpoint in (readyz, readiness_alias):
        assert endpoint(Mock())["status"] == "ready"
    monkeypatch.delenv(KEYRING_ENV)
    assert healthz(Mock())["status"] == "ok"


@pytest.fixture
def restored_db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    try:
        yield sessions
    finally:
        engine.dispose()


def control_row(identifier, *, legacy=False):
    row = Idempotency(
        id=identifier,
        tenant_id="synthetic-tenant",
        key=identifier,
        request_hash="test-hash",
        resource_id="resource-" + identifier,
    )
    payload = {"status": "DONE", "synthetic_value": "must-not-be-in-output"}
    row.response_json = (
        json.dumps(payload)
        if legacy
        else seal_control_response(
            payload,
            tenant_id=row.tenant_id,
            storage_key=row.key,
            request_hash=row.request_hash,
            resource_id=row.resource_id,
        )
    )
    return row


def integration_row(identifier):
    row = IntegrationResult(
        id=identifier,
        tenant_id="synthetic-tenant",
        outbox_id="op-" + identifier,
        source="MAUTIC",
        result_key=identifier,
        created_at=datetime.now(UTC) - timedelta(days=180),
    )
    row.payload_json = seal_integration_result(
        {"count": 3},
        tenant_id=row.tenant_id,
        outbox_id=row.outbox_id,
        source=row.source,
        result_key=row.result_key,
    )
    return row


def test_stream_verifies_old_and_new_keys_and_counts_legacy_separately(
    restored_db, isolated_durable_result_keyring
):
    with restored_db() as session:
        session.add(control_row("old"))
        add_key(isolated_durable_result_keyring)
        session.add_all(
            [control_row("new"), control_row("legacy", legacy=True), integration_row("expired")]
        )
        session.commit()
    with restored_db() as session:
        before = session.scalar(select(Idempotency.response_json).where(Idempotency.id == "old"))
        report = backup.verify_restored_records(session)
        assert report == {
            "schema_version": 1,
            "control_records": 3,
            "integration_records": 1,
            "authenticated_encrypted_records": 3,
            "legacy_records": 1,
        }
        assert not session.new and not session.dirty and not session.deleted
        assert (
            session.scalar(select(Idempotency.response_json).where(Idempotency.id == "old"))
            == before
        )
        assert "must-not-be-in-output" not in json.dumps(report)
        session.rollback()


@pytest.mark.parametrize("damage", ["unknown-key", "tamper", "tenant-binding", "legacy-disabled"])
def test_restore_scan_never_skips_unreadable_or_expired_records(
    restored_db, isolated_durable_result_keyring, monkeypatch, damage
):
    row = integration_row("expired")
    if damage == "tamper":
        row.payload_json = '{"format":"bad"}'
    if damage == "tenant-binding":
        row.tenant_id = "different-tenant"
    with restored_db() as session:
        session.add(row)
        if damage == "legacy-disabled":
            session.add(control_row("legacy", legacy=True))
        session.commit()
    if damage == "unknown-key":
        isolated_durable_result_keyring.write_text(new_keyring_document())
    if damage == "legacy-disabled":
        monkeypatch.setenv("KLYROW_DURABLE_RESULT_LEGACY_READ_ENABLED", "false")
    with restored_db() as session:
        with pytest.raises(HTTPException):
            backup.verify_restored_records(session)
        session.rollback()


def test_rehearsal_rejects_dirty_sessions_before_flush(restored_db):
    with restored_db() as session:
        session.add(control_row("pending"))
        with pytest.raises(backup.DurableBackupError):
            backup.verify_restored_records(session)
        session.rollback()


def test_readonly_postgres_transaction_and_cli_rollback(monkeypatch, capsys):
    # Verify the actual transaction instruction; real PostgreSQL CI is separate.
    session = Mock(new=set(), dirty=set(), deleted=set())
    session.get_bind.return_value.dialect.name = "postgresql"
    session.scalars.return_value = []
    backup.verify_restored_records(session)
    assert (
        str(session.execute.call_args.args[0])
        == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
    )
    from apps.gateway.app import main as core

    manager = Mock()
    manager.__enter__ = Mock(return_value=session)
    manager.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(core, "DB", lambda: manager)
    monkeypatch.setattr(
        backup,
        "verify_restored_records",
        lambda s: (_ for _ in ()).throw(ValueError("secret-record")),
    )
    assert backup.main() == 1
    session.rollback.assert_called_once()
    session.commit.assert_not_called()
    assert capsys.readouterr().err == "DURABLE_RESULT_RESTORE=BLOCKED\n"


def test_rehearsal_synthetic_secret_projections_are_readable_only_by_runtime_uid():
    script = (ROOT / "scripts/restore-verify").read_text()
    assert "install -m 0400 -o 10001 -g 10001" in script
    assert 'chown 10001:10001 "$stage/session-secret" "$stage/database-runtime-password"' in script
    assert 'chmod 0400 "$stage/session-secret" "$stage/database-runtime-password"' in script
