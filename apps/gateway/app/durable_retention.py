"""Explicit operator maintenance. No request/startup/worker path invokes a purge.

Payload tombstones retain authenticated replay digests and operation identities.
Holds and purges serialize on the same tenant-owned outbox lock as callbacks.
The caller must own the transaction and commit/rollback the complete operation.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .main import audit
from .durable_keys import load_keyring
from .durable_results import (
    FORMAT,
    canonical,
    integration_record,
    result_retention_seconds,
    seal_result_tombstone,
)
from .operations import (
    AccountClosure,
    IntegrationOutbox,
    IntegrationResult,
    IntegrationResultHold,
    locked_retention_tenant,
)

_SCOPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def clean_session(session: Session) -> None:
    if session.new or session.dirty or session.deleted:
        raise ValueError("retention_requires_clean_session")


def scope(value: str) -> str:
    if not isinstance(value, str) or not _SCOPE.fullmatch(value):
        raise ValueError("invalid_retention_scope")
    return value


def digest(value: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ValueError("invalid_change_reference")
    return value


def utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("aware_retention_timestamp_required")
    return value.astimezone(UTC)


def owned_outbox(session: Session, tenant_id: str, outbox_id: str):
    item = session.scalar(
        select(IntegrationOutbox)
        .where(IntegrationOutbox.tenant_id == tenant_id, IntegrationOutbox.id == outbox_id)
        .with_for_update(nowait=True)
        .execution_options(populate_existing=True)
    )
    if item is None:
        raise ValueError("retention_operation_not_found")
    return item


def result_hold(
    session: Session,
    *,
    tenant_id: str,
    outbox_id: str,
    hold_id: str,
    change_sha256: str,
    release: bool = False,
    apply: bool = False,
) -> dict:
    clean_session(session)
    for value in (tenant_id, outbox_id, hold_id):
        scope(value)
    digest(change_sha256)
    if type(release) is not bool or type(apply) is not bool:
        raise ValueError("invalid_hold_mode")
    locked_retention_tenant(session, tenant_id)
    owned_outbox(session, tenant_id, outbox_id)
    hold = session.scalar(
        select(IntegrationResultHold)
        .where(IntegrationResultHold.id == hold_id)
        .with_for_update(nowait=True)
        .execution_options(populate_existing=True)
    )
    if hold is not None and (hold.tenant_id != tenant_id or hold.outbox_id != outbox_id):
        raise ValueError("retention_hold_not_found")
    if release:
        if hold is None:
            raise ValueError("retention_hold_not_found")
        changed = hold.state == "ACTIVE"
        if not changed and hold.release_sha256 != change_sha256:
            raise ValueError("retention_hold_reference_conflict")
    else:
        if hold is not None and (hold.state != "ACTIVE" or hold.change_sha256 != change_sha256):
            raise ValueError("retention_hold_reference_conflict")
        changed = hold is None
    if apply and changed:
        if release:
            hold.state, hold.released_at, hold.release_sha256 = (
                "RELEASED",
                datetime.now(UTC),
                change_sha256,
            )
        else:
            session.add(
                IntegrationResultHold(
                    id=hold_id,
                    tenant_id=tenant_id,
                    outbox_id=outbox_id,
                    state="ACTIVE",
                    change_sha256=change_sha256,
                )
            )
        audit(
            session,
            {"tenant": tenant_id, "sub": "offline-retention-operator"},
            "integration.result.hold." + ("released:" if release else "placed:") + change_sha256,
        )
        session.flush()
    return {
        "schema_version": 1,
        "hold_id": hold_id,
        "would_change": changed,
        "updated": int(apply and changed),
        "applied": apply,
    }


def purge_plan_policy(*, tenant_id: str, before: datetime, after_id: str, limit: int) -> dict:
    scope(tenant_id)
    if not isinstance(after_id, str):
        raise ValueError("invalid_retention_cursor")
    if after_id:
        scope(after_id)
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("invalid_retention_limit")
    return {
        "schema_version": 1,
        "scope": "integration-result-payloads",
        "tenant_id": tenant_id,
        "before": utc(before).isoformat(),
        "after_id": after_id,
        "limit": limit,
        "retention_seconds": result_retention_seconds(),
    }


def purge_result_batch(
    session: Session,
    *,
    tenant_id: str,
    before: datetime,
    after_id: str = "",
    limit: int = 100,
    apply: bool = False,
    expected_plan_sha256: str = "",
    current: datetime | None = None,
) -> dict:
    """NOWAIT, never SKIP LOCKED: a busy row must not disappear behind the cursor."""
    clean_session(session)
    if type(apply) is not bool:
        raise ValueError("invalid_retention_mode")
    current = utc(current or datetime.now(UTC))
    policy = purge_plan_policy(tenant_id=tenant_id, before=before, after_id=after_id, limit=limit)
    if utc(before) > current - timedelta(seconds=policy["retention_seconds"]):
        raise ValueError("retention_cutoff_too_recent")
    locked_retention_tenant(session, tenant_id)
    tenant_held = (
        session.scalar(
            select(AccountClosure.id)
            .where(
                AccountClosure.tenant_id == tenant_id,
                AccountClosure.retention_policy == "LEGAL_HOLD",
            )
            .limit(1)
        )
        is not None
    )
    before = utc(before)
    authority = load_keyring()
    # Snapshot candidate identities only; refresh the actual result after locking
    # its owning outbox. This is the callback/hold/purge common lock order.
    candidate_ids = session.scalars(
        select(IntegrationResult.id)
        .where(
            IntegrationResult.tenant_id == tenant_id,
            IntegrationResult.created_at <= before,
            IntegrationResult.id > after_id,
        )
        .order_by(IntegrationResult.id)
        .limit(limit)
    ).all()
    proposed, skipped = [], {"held": 0, "nonterminal": 0, "ambiguous": 0, "purged": 0}
    for identity in candidate_ids:
        row = session.scalar(
            select(IntegrationResult).where(
                IntegrationResult.id == identity, IntegrationResult.tenant_id == tenant_id
            )
        )
        if row is None:
            raise ValueError("retention_result_changed")
        item = owned_outbox(session, tenant_id, row.outbox_id)
        row = session.scalar(
            select(IntegrationResult)
            .where(
                IntegrationResult.id == identity,
                IntegrationResult.tenant_id == tenant_id,
                IntegrationResult.outbox_id == item.id,
            )
            .with_for_update(nowait=True)
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise ValueError("retention_result_changed")
        created = (
            row.created_at
            if row.created_at.tzinfo is not None
            else row.created_at.replace(tzinfo=UTC)
        )
        if created > before:
            raise ValueError("retention_result_changed")
        if item.state != "COMPLETED" or item.lease_expires_at is not None:
            skipped["nonterminal"] += 1
            continue
        if (
            row.source != item.target
            or row.source not in {"MAUTIC", "N8N", "ODOO"}
            or session.scalar(
                select(IntegrationResult.id)
                .where(
                    IntegrationResult.tenant_id == tenant_id,
                    IntegrationResult.outbox_id == item.id,
                    IntegrationResult.source == "MAUTIC_LATE",
                )
                .limit(1)
            )
        ):
            skipped["ambiguous"] += 1
            continue
        if tenant_held or session.scalar(
            select(IntegrationResultHold.id)
            .where(
                IntegrationResultHold.tenant_id == tenant_id,
                IntegrationResultHold.outbox_id == item.id,
                IntegrationResultHold.state == "ACTIVE",
            )
            .limit(1)
        ):
            skipped["held"] += 1
            continue
        if json.loads(row.payload_json).get("format") != FORMAT:
            raise ValueError("legacy_result_requires_rewrap")
        document = integration_record(row)
        if document["schema_version"] == 2:
            skipped["purged"] += 1
            continue
        proposed.append((row, hashlib.sha256(row.payload_json.encode()).hexdigest()))
    # Bind approval to the exact eligible ciphertext set as well as the scope.
    # Any intervening result change or key rewrap demands a fresh dry-run plan.
    plan = {"policy": policy, "eligible": [[row.id, checksum] for row, checksum in proposed]}
    plan_sha = hashlib.sha256(canonical(plan, limit=1024 * 1024)).hexdigest()
    if apply and not secrets.compare_digest(digest(expected_plan_sha256), plan_sha):
        raise ValueError("retention_plan_changed")
    policy_sha = hashlib.sha256(canonical(policy)).hexdigest()
    changes = (
        [
            (row, seal_result_tombstone(row, current=current, policy_sha256=policy_sha))
            for row, _ in proposed
        ]
        if apply
        else []
    )
    if load_keyring() != authority or result_retention_seconds() != policy["retention_seconds"]:
        raise ValueError("retention_authority_changed")
    if apply:
        for row, tombstone in changes:
            row.payload_json = tombstone
        if changes:
            audit(
                session,
                {"tenant": tenant_id, "sub": "offline-retention-operator"},
                "integration.result.payloads_purged:" + plan_sha,
            )
        session.flush()
    return {
        "schema_version": 1,
        "scanned": len(candidate_ids),
        "eligible": len(proposed),
        "updated": len(changes) if apply else 0,
        "skipped": skipped,
        "applied": apply,
        "plan_sha256": plan_sha,
        "next_after_id": candidate_ids[-1] if candidate_ids else None,
        "more_may_exist": len(candidate_ids) == limit,
    }
