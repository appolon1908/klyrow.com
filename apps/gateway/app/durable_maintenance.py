"""Explicit, tenant-bounded rewrap. Never invoked by request handling or startup."""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from .main import Idempotency
from .durable_keys import load_keyring
from .durable_results import (
    FORMAT,
    read_control_response,
    rewrap_integration_result,
    seal_control_response,
)
from .operations import IntegrationResult


def rewrap_batch(
    session: Session,
    *,
    table: str,
    tenant_id: str,
    expected_key_id: str,
    after_id: str = "",
    limit: int = 100,
    apply: bool = False,
) -> dict:
    """Caller commits or rolls back; a corrupt row fails the entire batch.

    No SKIP LOCKED: skipping a busy row would make a keyset cursor silently omit it.
    PostgreSQL NOWAIT instead rejects a conflicting maintenance transaction.
    """
    if (
        table not in {"control", "integration"}
        or not tenant_id
        or type(limit) is not int
        or not 1 <= limit <= 1000
    ):
        raise ValueError("invalid_rewrap_scope")
    authority = load_keyring()
    if authority.active_key_id != expected_key_id:
        raise ValueError("rewrap_key_authority_changed")
    model = Idempotency if table == "control" else IntegrationResult
    field = "response_json" if table == "control" else "payload_json"
    query = (
        select(model)
        .where(model.tenant_id == tenant_id, model.id > after_id)
        .order_by(model.id)
        .limit(limit)
    )
    rows = session.scalars(
        query.with_for_update(nowait=True).execution_options(populate_existing=True)
    ).all()
    changes = []
    for row in rows:
        # Even rows already using the active ID must authenticate successfully.
        if table == "control":
            value = read_control_response(row)
            encoded = seal_control_response(
                value,
                tenant_id=row.tenant_id,
                storage_key=row.key,
                request_hash=row.request_hash,
                resource_id=row.resource_id,
            )
        else:
            encoded = rewrap_integration_result(row)
        original = json.loads(getattr(row, field))
        if original.get("format") != FORMAT or original.get("kid") != expected_key_id:
            changes.append((row, encoded))
    if load_keyring() != authority:
        raise ValueError("rewrap_key_authority_changed")
    if apply:
        for row, encoded in changes:
            setattr(row, field, encoded)
        session.flush()
    return {
        "schema_version": 1,
        "table": table,
        "scanned": len(rows),
        "eligible": len(changes),
        "updated": len(changes) if apply else 0,
        "applied": apply,
        "next_after_id": rows[-1].id if rows else None,
        "more_may_exist": len(rows) == limit,
    }
