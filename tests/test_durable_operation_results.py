"""Synthetic result/claim invariants; PostgreSQL cases run in required CI."""
from __future__ import annotations

import base64
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select, text, func
from sqlalchemy.orm import sessionmaker

from apps.gateway.app.durable_keys import KEYRING_ENV, KeyringError, load_keyring, parse_keyring
from apps.gateway.app.durable_results import (
    FORMAT, canonical, integration_document, read_control_response, redact_result,
    result_matches, result_readback, seal_control_response, seal_integration_result,
)
from apps.gateway.app.main import Base, Idempotency, Tenant
from apps.gateway.app.mautic_adapter import MauticAdapterState, _claim, _failure, _success
from apps.gateway.app.operations import IntegrationOutbox, IntegrationResult, ResultIn, result as accept_result
from apps.gateway.app.production_api import _operation_json, operation_cancel, operation_reconcile


CTX = {"tenant": "tenant-a", "sub": "operator-a", "role": "MARKETING"}


def store_control(value):
    row = SimpleNamespace(tenant_id="tenant-a", key="scoped-key-a", request_hash="hash-a", resource_id="operation-a")
    row.response_json = seal_control_response(value, tenant_id=row.tenant_id, storage_key=row.key,
                                              request_hash=row.request_hash, resource_id=row.resource_id)
    return row


def test_control_response_is_encrypted_and_independent_of_browser_rotation(monkeypatch):
    row = store_control({"status": "CANCELLED", "original": "synthetic-sensitive-result"})
    assert "synthetic-sensitive-result" not in row.response_json
    monkeypatch.setenv("KLYROW_SESSION_SECRET", "a-different-session-secret" * 4)
    assert read_control_response(row)["original"] == "synthetic-sensitive-result"


@pytest.mark.parametrize("field", ["tenant_id", "key", "request_hash", "resource_id"])
def test_ciphertext_cannot_move_between_identity_bindings(field):
    row = store_control({"status": "CANCELLED"})
    setattr(row, field, "different-identity")
    with pytest.raises(HTTPException) as error:
        read_control_response(row)
    assert (error.value.status_code, error.value.detail) == (503, "durable_result_unavailable")


def test_rotation_reads_previous_key_and_unknown_key_does_not_fall_back(isolated_durable_result_keyring):
    path = isolated_durable_result_keyring
    old = store_control({"status": "CANCELLED"})
    keys = json.loads(path.read_text())
    previous_id = keys["active_key_id"]
    keys["keys"]["next"] = base64.urlsafe_b64encode(os.urandom(32)).decode()
    keys["active_key_id"] = "next"
    path.write_text(json.dumps(keys))
    assert read_control_response(old) == {"status": "CANCELLED"}
    current = store_control({"status": "QUEUED"})
    assert json.loads(current.response_json)["kid"] == "next"
    del keys["keys"][previous_id]
    path.write_text(json.dumps(keys))
    with pytest.raises(HTTPException):
        read_control_response(old)
    assert read_control_response(current)["status"] == "QUEUED"


@pytest.mark.parametrize("damage", ["ciphertext", "nonce", "kid", "format"])
def test_damaged_envelope_never_becomes_a_new_request(damage):
    row = store_control({"status": "CANCELLED"})
    value = json.loads(row.response_json)
    value[damage] = "not-valid"
    row.response_json = json.dumps(value)
    with pytest.raises(HTTPException):
        read_control_response(row)


def test_missing_key_has_no_session_key_or_plaintext_fallback(monkeypatch):
    monkeypatch.delenv(KEYRING_ENV)
    with pytest.raises(HTTPException):
        store_control({"status": "CANCELLED"})


@pytest.mark.parametrize("kind", ["symlink", "directory", "writable", "oversized", "relative"])
def test_invalid_key_files_fail_closed(tmp_path, kind):
    if kind == "symlink" and os.name == "nt":
        pytest.skip("Windows requires elevated privileges for symlink creation")
    path = tmp_path / "bad"
    if kind == "symlink":
        path.symlink_to(tmp_path / "missing")
    elif kind == "directory":
        path.mkdir()
    else:
        path.write_text("x" * (9000 if kind == "oversized" else 1))
        path.chmod(0o666 if kind == "writable" else 0o600)
    with pytest.raises(KeyringError):
        load_keyring("relative.json" if kind == "relative" else path)


def test_duplicate_key_identifiers_fail_closed():
    with pytest.raises(KeyringError):
        parse_keyring(b'{"schema_version":1,"active_key_id":"a","keys":{"a":"x","a":"y"}}')


@pytest.mark.parametrize("flag", ["O_NOFOLLOW", "O_NONBLOCK"])
@pytest.mark.parametrize("availability", ["missing", "zero"])
def test_keyring_rejects_unavailable_protection_before_opening(
    isolated_durable_result_keyring, monkeypatch, flag, availability,
):
    if os.name == "nt":
        pytest.skip("POSIX file-flag protections are unavailable on Windows")
    if availability == "missing":
        monkeypatch.delattr(os, flag, raising=False)
    else:
        monkeypatch.setattr(os, flag, 0, raising=False)

    def unsafe_open(*args, **kwargs):
        pytest.fail("key file opened without required filesystem protection")

    monkeypatch.setattr(os, "open", unsafe_open)
    with pytest.raises(KeyringError, match="^durable_result_keyring_unavailable$"):
        load_keyring(isolated_durable_result_keyring)


def test_legacy_compatibility_is_read_only_and_can_be_disabled(monkeypatch):
    row = SimpleNamespace(tenant_id="a", key="b", request_hash="c", resource_id="d", response_json='{"ok":true}')
    assert read_control_response(row) == {"ok": True}
    monkeypatch.setenv("KLYROW_DURABLE_RESULT_LEGACY_READ_ENABLED", "false")
    with pytest.raises(HTTPException):
        read_control_response(row)


def result_row(payload=None, **changes):
    attrs = dict(id="result-a", tenant_id="tenant-a", outbox_id="operation-a", source="MAUTIC",
                 result_key="mautic:operation-a", created_at=datetime.now(timezone.utc))
    attrs.update(changes)
    row = IntegrationResult(**attrs)
    row.payload_json = seal_integration_result(payload or {"contacts": {"total": 1}}, tenant_id=row.tenant_id,
                                              outbox_id=row.outbox_id, source=row.source, result_key=row.result_key)
    return row


def test_nested_provider_secrets_are_redacted_and_storage_is_encrypted():
    original = {"contacts": {"total": 1}, "nested": [{"access_token": "token-value", "email": "private@example.test"}],
                "private-key": "key-value", "reset_url": "https://example.test/secret"}
    row = result_row(original)
    assert all(secret not in row.payload_json for secret in ("token-value", "private@example.test", "key-value"))
    payload, metadata = result_readback(row)
    assert metadata["availability"] == "AVAILABLE"
    assert payload["contacts"]["total"] == 1
    assert payload["nested"][0] == {"access_token": "[REDACTED]", "email": "[REDACTED]"}
    assert result_matches(row, original)
    assert not result_matches(row, {**original, "private-key": "changed"})


@pytest.mark.parametrize("kind", ["absent", "corrupt", "expired", "invalid-retention"])
def test_missing_or_unusable_result_has_explicit_status(monkeypatch, kind):
    row = result_row()
    if kind == "absent": row = None
    if kind == "corrupt": row.payload_json = "not-json"
    if kind == "expired": row.created_at -= timedelta(days=31)
    if kind == "invalid-retention": monkeypatch.setenv("KLYROW_RESULT_RETENTION_SECONDS", "-1")
    payload, metadata = result_readback(row)
    assert payload == {}
    assert metadata["availability"] == {"absent": "UNAVAILABLE", "corrupt": "INVALID", "expired": "EXPIRED", "invalid-retention": "INVALID"}[kind]


def test_oversized_and_deep_results_are_rejected():
    with pytest.raises(ValueError): canonical({"value": "x" * 65536})
    value = {}
    for _ in range(15): value = {"nested": value}
    with pytest.raises(ValueError): redact_result(value)


@pytest.fixture
def operation_db():
    url = os.getenv("KLYROW_CONTRACT_POSTGRES_URL")
    admin = None
    if url:
        schema = "durable_" + uuid.uuid4().hex
        admin = create_engine(url)
        with admin.begin() as connection: connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    else:
        engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions() as session:
        session.add(Tenant(id="tenant-a", name="Synthetic", quota=100))
        session.commit()
    try: yield sessions
    finally:
        engine.dispose()
        if admin is not None:
            with admin.begin() as connection: connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            admin.dispose()


def operation(state="PROCESSING", attempts=1, **changes):
    attrs = dict(id="operation-a", tenant_id="tenant-a", target="MAUTIC", event_type="campaign.publish.v1",
                 aggregate_id="campaign-a", payload_json='{"envelope":{"correlation_id":"original-correlation"}}',
                 idempotency_key="storage-digest-not-correlation", state=state, attempts=attempts,
                 lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=1))
    attrs.update(changes)
    return IntegrationOutbox(**attrs)


@pytest.mark.parametrize("state,attempt,expected", [("CANCELLED",1,1),("PROCESSING",2,1),("DEAD_LETTER",1,1)])
def test_stale_completion_retains_state_and_records_only_one_observation(operation_db, state, attempt, expected):
    with operation_db() as session:
        item = operation(state, attempt)
        session.add(item); session.commit()
        assert _success(session, item.id, {"ok":True}, expected_attempt=expected) is False
        assert _success(session, item.id, {"ok":True}, expected_attempt=expected) is False
        session.refresh(item)
        assert (item.state, item.attempts) == (state, attempt)
        assert session.scalar(select(func.count()).select_from(IntegrationResult)) == 1
        assert session.scalar(select(IntegrationResult.source)) == "MAUTIC_LATE"


def test_stale_failure_cannot_modify_new_claim_or_circuit(operation_db):
    with operation_db() as session:
        item = operation(attempts=2)
        session.add(item); session.commit()
        assert _failure(session,item.id,"old error",retryable=True,expected_attempt=1) is False
        assert item.state == "PROCESSING" and item.attempts == 2
        assert session.get(MauticAdapterState,"primary") is None


def test_valid_claim_completes_once_with_private_result(operation_db):
    with operation_db() as session:
        item = operation()
        session.add(item); session.commit()
        assert _success(session,item.id,{"count":1,"api_key":"private"},expected_attempt=1) is True
        assert _success(session,item.id,{"count":1},expected_attempt=1) is False
        assert item.state == "COMPLETED"
        result = session.scalar(select(IntegrationResult))
        assert integration_document(result)[0] == {"count":1,"api_key":"[REDACTED]"}
        assert session.scalar(select(func.count()).select_from(IntegrationResult)) == 1


def test_expired_claim_cannot_complete(operation_db):
    with operation_db() as session:
        item=operation(lease_expires_at=datetime.now(timezone.utc)-timedelta(seconds=1))
        session.add(item); session.commit()
        assert _success(session,item.id,{"ok":True},expected_attempt=1) is False
        assert item.state == "PROCESSING"


def test_cancelled_operation_replays_after_browser_key_rotation(operation_db, monkeypatch):
    with operation_db() as session:
        item=operation("PENDING",0)
        session.add(item); session.commit()
        first=operation_cancel(item.id,CTX,session,"cancel-key-a")
        monkeypatch.setenv("KLYROW_SESSION_SECRET","different-browser-secret" * 2)
        assert operation_cancel(item.id,CTX,session,"cancel-key-a") == first
        assert first["status"] == "CANCELLED"
        assert '"format"' in session.scalar(select(Idempotency.response_json))


def test_ambiguous_dead_letter_cannot_be_blindly_requeued(operation_db):
    with operation_db() as session:
        item=operation("DEAD_LETTER",1,last_error="mautic_transport_ambiguous")
        session.add(item);session.commit()
        with pytest.raises(HTTPException) as error: operation_reconcile(item.id,CTX,session,"reconcile-key-a")
        assert error.value.detail == "operation_requires_provider_readback"
        assert item.state == "DEAD_LETTER"


def test_integration_result_is_encrypted_and_changed_replay_conflicts(operation_db):
    with operation_db() as session:
        item=operation("PENDING",0,target="N8N")
        session.add(item);session.commit()
        request=ResultIn(outbox_id=item.id,source="N8N",result_key="result-key-1",payload={"ok":True,"token":"secret-a"})
        first=accept_result(request,CTX,session)
        assert accept_result(request,CTX,session) == {"id":first["id"],"duplicate":True}
        with pytest.raises(HTTPException) as error:
            accept_result(request.model_copy(update={"payload":{"ok":True,"token":"secret-b"}}),CTX,session)
        assert error.value.status_code == 409
        assert "secret-a" not in session.scalar(select(IntegrationResult.payload_json))


@pytest.mark.skipif(not os.getenv("KLYROW_CONTRACT_POSTGRES_URL"), reason="Required CI supplies disposable PostgreSQL")
def test_locked_cancel_and_claim_have_one_winner(operation_db):
    from concurrent.futures import ThreadPoolExecutor
    with operation_db() as initial:
        initial.add(operation("PENDING",0));initial.commit()
    with operation_db() as cancellation:
        item=cancellation.scalar(select(IntegrationOutbox).where(IntegrationOutbox.id=="operation-a").with_for_update())
        def competing_claim():
            with operation_db() as worker: return _claim(worker)
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(competing_claim).result(timeout=10) is None
        assert operation_cancel(item.id,CTX,cancellation,"concurrent-cancel-key")["status"] == "CANCELLED"
    with operation_db() as verify:
        assert _claim(verify) is None
        assert verify.get(IntegrationOutbox,"operation-a").state == "CANCELLED"


def test_late_completion_blocks_reclaim_and_reports_reconciliation(operation_db):
    with operation_db() as session:
        item = operation("RETRY", 1, next_attempt_at=datetime.now(timezone.utc)-timedelta(seconds=1))
        session.add(item); session.commit()
        assert _success(session, item.id, {"ok": True}, expected_attempt=1) is False
        assert _claim(session) is None
        view = _operation_json(item, session)
        assert view["reconciliation_required"] and not view["retryability"]
        assert item.state == "RETRY"


def test_duplicate_envelope_fields_fail_closed():
    row = store_control({"ok": True})
    row.response_json = row.response_json[:-1] + ',"format":"klyrow-durable-result.v1"}'
    with pytest.raises(HTTPException): read_control_response(row)


def test_maximum_accepted_payload_has_room_for_authenticated_metadata():
    value = {"value": "x" * (65536-len(canonical({"value": ""})))}
    assert len(canonical(value)) == 65536
    assert result_matches(result_row(value), value)


def test_tenant_bounded_rewrap_is_dry_run_idempotent_and_preserves_hash(operation_db, isolated_durable_result_keyring):
    from apps.gateway.app.durable_maintenance import rewrap_batch
    key_id = load_keyring().active_key_id
    original = {"ok": True, "token": "must-still-affect-replay-hash"}
    with operation_db() as session:
        session.add(operation("COMPLETED"))
        row = result_row(original)
        row.payload_json = json.dumps(original)
        session.add(row)
        session.add(IntegrationResult(id="result-b", tenant_id="tenant-b", outbox_id="other",
            source="MAUTIC", result_key="other-key", payload_json='{"private":"other-tenant"}'))
        session.commit()
        report = rewrap_batch(session, table="integration", tenant_id="tenant-a", expected_key_id=key_id)
        assert report["eligible"] == 1 and report["updated"] == 0
        assert row.payload_json == json.dumps(original)
        report = rewrap_batch(session, table="integration", tenant_id="tenant-a", expected_key_id=key_id, apply=True)
        session.commit()
        assert report["updated"] == 1 and result_matches(row, original)
        assert session.get(IntegrationResult, "result-b").payload_json == '{"private":"other-tenant"}'
        assert rewrap_batch(session, table="integration", tenant_id="tenant-a", expected_key_id=key_id, apply=True)["updated"] == 0
        keys = json.loads(isolated_durable_result_keyring.read_text())
        keys["keys"]["rotated"] = base64.urlsafe_b64encode(os.urandom(32)).decode()
        keys["active_key_id"] = "rotated"
        isolated_durable_result_keyring.write_text(json.dumps(keys))
        assert rewrap_batch(session, table="integration", tenant_id="tenant-a", expected_key_id="rotated", apply=True)["updated"] == 1
        session.commit()
        del keys["keys"][key_id]
        isolated_durable_result_keyring.write_text(json.dumps(keys))
        assert result_matches(row, original)
        assert not result_matches(row, {**original, "token":"changed"})


def test_rewrap_corrupt_batch_changes_no_row(operation_db):
    from apps.gateway.app.durable_maintenance import rewrap_batch
    with operation_db() as session:
        session.add_all([
            Idempotency(id="a", tenant_id="tenant-a", key="a", request_hash="a", resource_id="a", response_json='{"ok":true}'),
            Idempotency(id="b", tenant_id="tenant-a", key="b", request_hash="b", resource_id="b", response_json='broken'),
        ])
        session.commit()
        with pytest.raises(HTTPException):
            rewrap_batch(session, table="control", tenant_id="tenant-a", expected_key_id=load_keyring().active_key_id, apply=True)
        session.rollback()
        assert session.get(Idempotency, "a").response_json == '{"ok":true}'


@pytest.mark.parametrize("field", ["email_address", "phoneNumber", "avatar_url", "signed_url", "html_content", "rawResponse"])
def test_common_composite_provider_fields_are_redacted(field):
    payload = {"nested": {field: "synthetic-private-value", "count": 7}}
    row = result_row(payload)
    value, metadata = result_readback(row)
    assert metadata["availability"] == "AVAILABLE"
    assert value == {"nested": {field: "[REDACTED]", "count": 7}}
    assert result_matches(row, payload)


@pytest.mark.parametrize("surface,max_queries", [("operations", 4), ("mautic_operations", 3)])
@pytest.mark.parametrize("size", [4, 40])
def test_operation_lists_bulk_load_results_and_keep_tenant_and_correlation_guards(operation_db, monkeypatch, surface, max_queries, size):
    from sqlalchemy import event
    from apps.gateway.app import production_api as api
    from apps.gateway.app.runtime_authority_fixes import operation_json_with_correlation
    monkeypatch.setattr(api, "_operation_json", operation_json_with_correlation)
    with operation_db() as session:
        session.add(Tenant(id="tenant-b", name="Foreign", quota=100))
        current = datetime.now(timezone.utc)
        for index in range(size):
            identity = f"batch-{index}"
            item = operation("COMPLETED" if index % 2 == 0 else "PENDING", id=identity,
                             idempotency_key=f"batch-key-{index}")
            session.add(item)
            if index % 2 == 0:
                for version in (0, 1):
                    key = f"result-{index}-{version}"
                    session.add(IntegrationResult(id=key, tenant_id="tenant-a", outbox_id=identity,
                        source="MAUTIC", result_key=key, created_at=current + timedelta(seconds=version),
                        payload_json=seal_integration_result({"version": version}, tenant_id="tenant-a",
                            outbox_id=identity, source="MAUTIC", result_key=key)))
        session.add(IntegrationResult(id="late-batch", tenant_id="tenant-a", outbox_id="batch-1",
            source="MAUTIC_LATE", result_key="late-batch", payload_json="{}"))
        session.add(IntegrationResult(id="foreign-late", tenant_id="tenant-b", outbox_id="batch-0",
            source="MAUTIC_LATE", result_key="foreign-late", payload_json="{}"))
        session.add(IntegrationResult(id="wrong-source", tenant_id="tenant-a", outbox_id="batch-0",
            source="ODOO", result_key="wrong-source", payload_json="{}", created_at=current + timedelta(days=1)))
        session.commit()
        statements = []
        def observe(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)
        event.listen(session.bind, "before_cursor_execute", observe)
        try:
            if surface == "operations":
                document = api.operations(CTX, session, limit=200)
            else:
                document = api.mautic_operations(CTX, session)
        finally:
            event.remove(session.bind, "before_cursor_execute", observe)
        assert len(statements) <= max_queries, len(statements)
        rows = {row["operation_id"]: row for row in document["items"]}
        assert len(rows) == size
        assert rows["batch-0"]["result"] == {"version": 1}
        assert rows["batch-0"]["reconciliation_required"] is False
        assert rows["batch-1"]["reconciliation_required"] is True
        assert all(row["correlation_id"] == "original-correlation" for row in rows.values())


@pytest.mark.parametrize("surface", ["canonical", "legacy-admin"])
@pytest.mark.parametrize("state,late", [("DEAD_LETTER", False), ("DEAD_LETTER", True), ("RETRY", True)])
def test_every_recovery_surface_rejects_ambiguous_mautic(operation_db, surface, state, late):
    from apps.gateway.app.operations import RecoverIn, recover_integration
    with operation_db() as session:
        item = operation(state, last_error="worker_lease_expired_ambiguous")
        session.add(item)
        if late:
            session.add(result_row({"status": "RECONCILIATION_REQUIRED"}, source="MAUTIC_LATE"))
        session.commit()
        session.refresh(item)
        before = (item.state, item.attempts, item.last_error, item.updated_at)
        with pytest.raises(HTTPException) as denied:
            if surface == "canonical":
                operation_reconcile(item.id, CTX, session, "recovery-ambiguity-key")
            else:
                recover_integration(item.id, RecoverIn(reason="Synthetic recovery"),
                                    {**CTX, "role": "platform_admin"}, session)
        assert (denied.value.status_code, denied.value.detail) == (409, "operation_requires_provider_readback")
        session.rollback()
        session.refresh(item)
        assert (item.state, item.attempts, item.last_error, item.updated_at) == before
        assert session.scalar(select(func.count()).select_from(Idempotency)) == 0
        assert _claim(session) is None


@pytest.mark.parametrize("target,state", [("MAUTIC", "RETRY"), ("N8N", "RETRY"), ("ODOO", "DEAD_LETTER")])
def test_safe_legacy_recovery_still_works_without_cross_tenant_observations(operation_db, target, state):
    from apps.gateway.app.operations import RecoverIn, recover_integration
    with operation_db() as session:
        item = operation(state, target=target, last_error="connection_not_established")
        session.add(item)
        # A foreign tenant's observation must not change this tenant's authority.
        session.add(result_row({"ok": True}, tenant_id="tenant-b", source="MAUTIC_LATE"))
        session.commit()
        response = recover_integration(item.id, RecoverIn(reason="Connection restored"),
                                       {**CTX, "role": "platform_admin"}, session)
        assert response["state"] == "PENDING" and response["attempts"] == 1
        assert item.last_error is None


@pytest.mark.skipif(not os.getenv("KLYROW_CONTRACT_POSTGRES_URL"), reason="Required CI supplies disposable PostgreSQL")
@pytest.mark.parametrize("surface", ["canonical", "legacy-admin"])
def test_recovery_rechecks_observation_after_competing_writer_commits(operation_db, surface):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from apps.gateway.app.operations import RecoverIn, locked_integration_outbox, recover_integration
    with operation_db() as initial:
        initial.add(operation("RETRY", last_error="connection_not_established"))
        initial.commit()
    started = Event()
    def recover():
        with operation_db() as recovery:
            recovery.execute(text("SET LOCAL statement_timeout = '5s'"))
            started.set()
            try:
                if surface == "canonical":
                    operation_reconcile("operation-a", CTX, recovery, "competing-recovery-key")
                else:
                    recover_integration("operation-a", RecoverIn(reason="Synthetic retry"),
                                        {**CTX, "role": "platform_admin"}, recovery)
            except HTTPException as denied:
                recovery.rollback()
                return denied.status_code, denied.detail
            return 200, "unexpected_requeue"
    with ThreadPoolExecutor(max_workers=1) as pool:
        with operation_db() as writer:
            assert locked_integration_outbox(writer, "operation-a") is not None
            writer.add(result_row({"status": "RECONCILIATION_REQUIRED"}, source="MAUTIC_LATE"))
            writer.flush()
            pending = pool.submit(recover)
            assert started.wait(timeout=5)
            writer.commit()
        assert pending.result(timeout=10) == (409, "operation_requires_provider_readback")
    with operation_db() as verify:
        assert verify.get(IntegrationOutbox, "operation-a").state == "RETRY"
        assert verify.scalar(select(func.count()).select_from(Idempotency)) == 0
        assert _claim(verify) is None

# Operator-only retention: shared SQLite/PostgreSQL acceptance in required CI.
from apps.gateway.app.durable_retention import purge_result_batch, result_hold
from apps.gateway.app.durable_results import integration_record, seal, seal_result_tombstone, rewrap_integration_result
from apps.gateway.app.operations import AccountClosure, IntegrationResultHold
from apps.gateway.app.main import Audit

RETENTION_NOW = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)
RETENTION_BEFORE = RETENTION_NOW - timedelta(days=31)
CHANGE_SHA = "a" * 64
RELEASE_SHA = "b" * 64


def old_result(session, name="a", *, tenant="tenant-a", target="N8N", state="COMPLETED", age=40):
    item = operation(state, id="operation-" + name, tenant_id=tenant, target=target,
                     idempotency_key="idempotency-" + name, lease_expires_at=None)
    row = result_row({"count": 4, "email": "synthetic@example.test"}, id="result-" + name,
                     tenant_id=tenant, outbox_id=item.id, source=target, result_key="result-key-" + name,
                     created_at=RETENTION_NOW - timedelta(days=age))
    session.add_all([item, row]); session.commit()
    return item, row


def purge_kwargs(**changes):
    return {"tenant_id": "tenant-a", "before": RETENTION_BEFORE, "current": RETENTION_NOW, **changes}


def planned_purge(session, **changes):
    args = purge_kwargs(**changes)
    plan = purge_result_batch(session, **args)
    session.rollback()
    return purge_result_batch(session, **args, apply=True, expected_plan_sha256=plan["plan_sha256"])


def place_hold(session, item="operation-a", identity="hold-a", **changes):
    return result_hold(session, tenant_id="tenant-a", outbox_id=item, hold_id=identity,
                       change_sha256=CHANGE_SHA, apply=True, **changes)


def test_retention_dry_run_then_purge_preserves_replay_and_operation_truth(operation_db):
    with operation_db() as session:
        item, row = old_result(session)
        payload = row.payload_json
        original_item = (item.payload_json, item.idempotency_key, item.state, item.attempts)
        report = purge_result_batch(session, **purge_kwargs())
        assert report["eligible"] == 1 and report["updated"] == 0
        session.commit()  # Even a mistakenly committed dry-run writes nothing.
        assert session.get(IntegrationResult, row.id).payload_json == payload
        assert session.scalar(select(func.count()).select_from(Audit)) == 0
        applied = planned_purge(session)
        session.commit(); session.refresh(row); session.refresh(item)
        assert applied["updated"] == 1
        document = integration_record(row)
        assert set(document) == {"schema_version", "request_hash", "purged_at", "policy_sha256"}
        assert document["schema_version"] == 2
        assert (item.payload_json, item.idempotency_key, item.state, item.attempts) == original_item
        assert session.scalar(select(func.count()).select_from(IntegrationResult)) == 1
        payload, metadata = result_readback(row, current=RETENTION_NOW)
        assert payload == {} and metadata["availability"] == "PURGED"
        api = _operation_json(item, session)
        assert api["status"] == "SUCCEEDED" and not api["reconciliation_required"]
        assert api["result_metadata"]["availability"] == "PURGED" and api["error"] is None
        same = ResultIn(outbox_id=item.id, source="N8N", result_key=row.result_key,
                        payload={"count": 4, "email": "synthetic@example.test"})
        assert accept_result(same, CTX, session) == {"id": row.id, "duplicate": True}
        with pytest.raises(HTTPException) as conflict:
            accept_result(same.model_copy(update={"payload": {"count": 4, "email": "changed@example.test"}}), CTX, session)
        assert conflict.value.status_code == 409
        session.rollback()
        repeated = planned_purge(session)
        assert repeated["updated"] == 0 and repeated["skipped"]["purged"] == 1


def test_multiple_preservation_holds_need_individual_explicit_release(operation_db):
    with operation_db() as session:
        _, row = old_result(session)
        original = row.payload_json
        assert place_hold(session)["updated"] == 1
        session.commit()
        assert place_hold(session)["updated"] == 0
        session.commit()
        place_hold(session, identity="hold-b"); session.commit()
        assert planned_purge(session)["skipped"]["held"] == 1
        session.rollback()
        args = dict(tenant_id="tenant-a", outbox_id="operation-a", hold_id="hold-a", change_sha256=RELEASE_SHA, release=True)
        assert result_hold(session, **args)["updated"] == 0
        session.commit()
        assert session.get(IntegrationResultHold, "hold-a").state == "ACTIVE"
        assert result_hold(session, **args, apply=True)["updated"] == 1
        session.commit()
        assert result_hold(session, **args, apply=True)["updated"] == 0
        session.commit()
        assert planned_purge(session)["eligible"] == 0
        session.rollback()
        assert session.get(IntegrationResult, row.id).payload_json == original
        result_hold(session, **{**args, "hold_id": "hold-b"}, apply=True); session.commit()
        assert planned_purge(session)["updated"] == 1
        session.commit()
        with pytest.raises(ValueError, match="conflict"):
            place_hold(session)


@pytest.mark.parametrize("change", ["hold", "rewrap", "result-content", "retention-setting"])
def test_retention_plan_cannot_apply_after_its_eligible_set_or_authority_changes(operation_db, monkeypatch, change):
    with operation_db() as session:
        _, row = old_result(session)
        original = row.payload_json
        first = purge_result_batch(session, **purge_kwargs())
        session.rollback()
        row = session.get(IntegrationResult, "result-a")
        if change == "hold":
            place_hold(session); session.commit()
        elif change == "rewrap":
            row.payload_json = rewrap_integration_result(row); session.commit()
        elif change == "result-content":
            row.payload_json = seal_integration_result({"count": 2}, tenant_id=row.tenant_id,
                outbox_id=row.outbox_id, source=row.source, result_key=row.result_key)
            session.commit()
        else:
            monkeypatch.setenv("KLYROW_RESULT_RETENTION_SECONDS", str(29 * 86400))
        before_apply = row.payload_json
        with pytest.raises(ValueError, match="retention_plan_changed"):
            purge_result_batch(session, **purge_kwargs(), apply=True, expected_plan_sha256=first["plan_sha256"])
        session.rollback()
        assert session.get(IntegrationResult, row.id).payload_json == before_apply
        assert integration_record(row)["schema_version"] == 1


@pytest.mark.parametrize("kind", ["corrupt", "legacy", "missing-key", "unknown-key"])
def test_retention_bad_record_rolls_back_entire_batch(operation_db, monkeypatch, kind):
    with operation_db() as session:
        _, first = old_result(session)
        _, second = old_result(session, "b")
        if kind == "corrupt": second.payload_json = "not-json"
        if kind == "legacy": second.payload_json = '{"legacy":true}'
        if kind == "unknown-key":
            envelope = json.loads(second.payload_json); envelope["kid"] = "missing"
            second.payload_json = json.dumps(envelope)
        session.commit(); before = (first.payload_json, second.payload_json)
        if kind == "missing-key": monkeypatch.delenv(KEYRING_ENV)
        with pytest.raises((HTTPException, ValueError, KeyringError)):
            purge_result_batch(session, **purge_kwargs(), apply=True, expected_plan_sha256=CHANGE_SHA)
        session.rollback()
        assert (session.get(IntegrationResult, first.id).payload_json, session.get(IntegrationResult, second.id).payload_json) == before
        assert session.scalar(select(func.count()).select_from(Audit)) == 0


@pytest.mark.parametrize("state", ["PENDING", "PROCESSING", "RETRY", "DEAD_LETTER", "CANCELLED"])
def test_retention_never_erases_nonterminal_or_unreconciled_results(operation_db, state):
    with operation_db() as session:
        _, row = old_result(session, state=state)
        original = row.payload_json
        assert planned_purge(session)["skipped"]["nonterminal"] == 1
        session.commit()
        assert session.get(IntegrationResult, row.id).payload_json == original


@pytest.mark.parametrize("kind", ["late", "wrong-source", "lease", "young", "foreign-tenant", "tenant-hold"])
def test_retention_skips_evidence_outside_its_safe_scope(operation_db, kind):
    with operation_db() as session:
        item, row = old_result(session, target="MAUTIC", age=2 if kind == "young" else 40)
        if kind == "late": session.add(result_row({"late": True}, id="late-a", source="MAUTIC_LATE"))
        if kind == "wrong-source": row.source = "N8N"
        if kind == "lease": item.lease_expires_at = RETENTION_NOW + timedelta(days=1)
        if kind == "foreign-tenant": row.tenant_id = "tenant-b"
        if kind == "tenant-hold":
            session.add(AccountClosure(id="closure-a", tenant_id="tenant-a", requested_by="owner",
                confirmation_hash=CHANGE_SHA, grace_until=RETENTION_NOW, retention_policy="LEGAL_HOLD", state="CLOSED"))
        session.commit(); original = row.payload_json
        report = planned_purge(session)
        session.commit()
        assert report["eligible"] == report["updated"] == 0
        assert session.get(IntegrationResult, row.id).payload_json == original


def test_foreign_tenant_observation_and_hold_do_not_block_owned_purge(operation_db):
    with operation_db() as session:
        _, row = old_result(session, target="MAUTIC")
        session.add(result_row({"late": True}, id="late-b", tenant_id="tenant-b", source="MAUTIC_LATE"))
        session.add(AccountClosure(id="closure-b", tenant_id="tenant-b", requested_by="owner-b",
            confirmation_hash=CHANGE_SHA, grace_until=RETENTION_NOW, retention_policy="LEGAL_HOLD"))
        session.commit()
        assert planned_purge(session)["updated"] == 1
        session.commit()
        assert integration_record(row)["schema_version"] == 2


@pytest.mark.parametrize("changes", [{"tenant_id": "*"}, {"limit": True}, {"limit": 0}, {"limit": 1001},
    {"before": RETENTION_NOW}, {"before": datetime(2020, 1, 1)}, {"after_id": None}, {"after_id": "*"}, {"apply": "true"}])
def test_invalid_retention_scope_is_rejected(operation_db, changes):
    with operation_db() as session:
        old_result(session)
        with pytest.raises(ValueError): purge_result_batch(session, **purge_kwargs(**changes))
        session.rollback()
        assert integration_record(session.get(IntegrationResult, "result-a"))["schema_version"] == 1


@pytest.mark.parametrize("method", ["purge", "hold"])
def test_retention_rejects_dirty_sessions_before_any_autoflush(operation_db, method):
    with operation_db() as session:
        item, _ = old_result(session)
        item.last_error = "pending-change"
        with pytest.raises(ValueError, match="clean_session"):
            if method == "purge": purge_result_batch(session, **purge_kwargs())
            else: place_hold(session)
        session.rollback()
        assert session.get(IntegrationOutbox, item.id).last_error is None


def test_hold_is_tenant_bound_and_change_references_are_idempotent(operation_db):
    with operation_db() as session:
        old_result(session)
        session.add(Tenant(id="tenant-b", name="Other", quota=1)); session.commit()
        old_result(session, "b", tenant="tenant-b")
        place_hold(session); session.commit()
        with pytest.raises(ValueError, match="not_found"):
            result_hold(session, tenant_id="tenant-b", outbox_id="operation-b", hold_id="hold-a", change_sha256=CHANGE_SHA, release=True, apply=True)
        session.rollback()
        with pytest.raises(ValueError, match="conflict"):
            result_hold(session, tenant_id="tenant-a", outbox_id="operation-a", hold_id="hold-a", change_sha256=RELEASE_SHA, apply=True)
        session.rollback()
        assert session.get(IntegrationResultHold, "hold-a").state == "ACTIVE"


def test_tombstone_rotation_and_restore_verification_keep_replay_authority(operation_db, isolated_durable_result_keyring):
    from apps.gateway.app.durable_backup import verify_restored_records
    with operation_db() as session:
        _, row = old_result(session)
        planned_purge(session); session.commit()
        before = integration_record(row)
        path = isolated_durable_result_keyring
        keys = json.loads(path.read_text()); old_id = keys["active_key_id"]
        keys["keys"]["rotated"] = base64.urlsafe_b64encode(os.urandom(32)).decode(); keys["active_key_id"] = "rotated"
        path.write_text(json.dumps(keys))
        row.payload_json = rewrap_integration_result(row); session.commit()
        del keys["keys"][old_id]; path.write_text(json.dumps(keys))
        assert integration_record(row) == before and result_readback(row)[1]["availability"] == "PURGED"
        assert result_matches(row, {"count": 4, "email": "synthetic@example.test"})
        session.rollback()
        report = verify_restored_records(session)
        assert report["authenticated_encrypted_records"] == 1 and report["legacy_records"] == 0


@pytest.mark.parametrize("change", [{"schema_version": True}, {"schema_version": 3}, {"purged_at": "yesterday"},
    {"purged_at": "2020-01-01T00:00:00"}, {"policy_sha256": "bad"}, {"request_hash": "bad"}, {"result": {"unexpected": True}}])
def test_authenticated_but_invalid_tombstone_is_not_a_success(change):
    row = result_row()
    row.payload_json = seal_result_tombstone(row, current=RETENTION_NOW, policy_sha256=CHANGE_SHA)
    document = {**integration_record(row), **change}
    row.payload_json = seal(document, ["integration-result", row.tenant_id, row.outbox_id, row.source, row.result_key])
    assert result_readback(row)[1]["availability"] == "INVALID"
    with pytest.raises(HTTPException): result_matches(row, {"contacts": {"total": 1}})


def test_purge_cursor_reports_skips_and_requires_new_pass_after_hold_release(operation_db):
    with operation_db() as session:
        for name in ("a", "b", "c"): old_result(session, name)
        place_hold(session); session.commit()
        first = planned_purge(session, limit=1); session.commit()
        assert first["next_after_id"] == "result-a" and first["skipped"]["held"] == 1
        second = planned_purge(session, limit=2, after_id=first["next_after_id"]); session.commit()
        assert second["updated"] == 2
        assert integration_record(session.get(IntegrationResult, "result-a"))["schema_version"] == 1
        result_hold(session, tenant_id="tenant-a", outbox_id="operation-a", hold_id="hold-a",
                    change_sha256=RELEASE_SHA, release=True, apply=True); session.commit()
        assert planned_purge(session)["updated"] == 1


@pytest.mark.skipif(not os.getenv("KLYROW_CONTRACT_POSTGRES_URL"), reason="Required CI supplies disposable PostgreSQL")
@pytest.mark.parametrize("lock_owner", ["tenant", "outbox", "result"])
def test_postgres_retention_does_not_skip_locked_candidate_and_rechecks_hold(operation_db, lock_owner):
    from sqlalchemy.exc import OperationalError
    from apps.gateway.app.operations import locked_integration_outbox, locked_retention_tenant
    with operation_db() as setup: old_result(setup)
    with operation_db() as holder:
        model = {"tenant": Tenant, "outbox": IntegrationOutbox, "result": IntegrationResult}[lock_owner]
        identity = {"tenant": "tenant-a", "outbox": "operation-a", "result": "result-a"}[lock_owner]
        holder.scalar(select(model).where(model.id == identity).with_for_update())
        with operation_db() as candidate:
            candidate.execute(text("SET LOCAL statement_timeout = '3s'"))
            with pytest.raises(OperationalError): purge_result_batch(candidate, **purge_kwargs())
            candidate.rollback()
        holder.rollback()
    with operation_db() as holding:
        place_hold(holding); holding.commit()
    with operation_db() as candidate:
        assert planned_purge(candidate)["skipped"]["held"] == 1
        candidate.commit()


@pytest.mark.skipif(not os.getenv("KLYROW_CONTRACT_POSTGRES_URL"), reason="Required CI supplies disposable PostgreSQL")
@pytest.mark.parametrize("competing", ["hold", "tenant-hold", "callback"])
def test_postgres_purge_serializes_preservation_and_duplicate_callbacks(operation_db, competing):
    from sqlalchemy.exc import OperationalError
    from apps.gateway.app.operations import closure, ClosureIn
    with operation_db() as setup: old_result(setup)
    with operation_db() as purger:
        assert planned_purge(purger)["updated"] == 1
        with operation_db() as other:
            other.execute(text("SET LOCAL lock_timeout = '150ms'"))
            with pytest.raises(OperationalError):
                if competing == "hold": place_hold(other)
                elif competing == "tenant-hold": closure(ClosureIn(retention_policy="LEGAL_HOLD"), CTX, other)
                else:
                    accept_result(ResultIn(outbox_id="operation-a", source="N8N", result_key="result-key-a",
                        payload={"count": 4, "email": "synthetic@example.test"}), CTX, other)
            other.rollback()
        purger.commit()
    with operation_db() as later:
        # A later hold is not retroactive, but it cannot resurrect/purge a payload again.
        if competing == "hold": place_hold(later); later.commit()
        elif competing == "tenant-hold": closure(ClosureIn(retention_policy="LEGAL_HOLD"), CTX, later)
        else:
            assert accept_result(ResultIn(outbox_id="operation-a", source="N8N", result_key="result-key-a",
                payload={"count": 4, "email": "synthetic@example.test"}), CTX, later)["duplicate"]
        assert integration_record(later.get(IntegrationResult, "result-a"))["schema_version"] == 2


def test_retention_keyring_change_during_apply_rolls_back_before_writes(operation_db, monkeypatch, isolated_durable_result_keyring):
    import apps.gateway.app.durable_retention as retention
    with operation_db() as session:
        _, row = old_result(session)
        initial = row.payload_json
        plan = purge_result_batch(session, **purge_kwargs()); session.rollback()
        original = retention.seal_result_tombstone
        def rotate(*args, **kwargs):
            encoded = original(*args, **kwargs)
            keys = json.loads(isolated_durable_result_keyring.read_text())
            keys["keys"]["changed"] = base64.urlsafe_b64encode(os.urandom(32)).decode()
            keys["active_key_id"] = "changed"
            isolated_durable_result_keyring.write_text(json.dumps(keys))
            return encoded
        monkeypatch.setattr(retention, "seal_result_tombstone", rotate)
        with pytest.raises(ValueError, match="authority_changed"):
            purge_result_batch(session, **purge_kwargs(), apply=True, expected_plan_sha256=plan["plan_sha256"])
        session.rollback()
        assert session.get(IntegrationResult, "result-a").payload_json == initial


def test_retention_plan_supports_documented_large_batch_without_result_payload_limit(operation_db):
    with operation_db() as session:
        for index in range(400):
            name = f"batch-{index:04}-" + "x" * 130
            item = operation("COMPLETED", id="operation-" + name, target="N8N", idempotency_key=name, lease_expires_at=None)
            row = result_row({"count": 1}, id="result-" + name, outbox_id=item.id, source="N8N", result_key=name,
                             created_at=RETENTION_NOW - timedelta(days=40))
            session.add_all([item, row])
        session.commit()
        report = purge_result_batch(session, **purge_kwargs(limit=1000))
        assert report["eligible"] == 400 and not report["more_may_exist"]
        session.rollback()
