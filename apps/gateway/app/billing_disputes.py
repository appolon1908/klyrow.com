"""Provider-neutral dispute and chargeback evidence authority."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from fastapi import HTTPException


DISPUTE_STATES = {"OPEN", "EVIDENCE_REQUIRED", "SUBMITTED", "WON", "LOST", "CLOSED"}
DISPUTE_TRANSITIONS = {
    "OPEN": {"EVIDENCE_REQUIRED", "CLOSED"},
    "EVIDENCE_REQUIRED": {"SUBMITTED", "CLOSED"},
    "SUBMITTED": {"WON", "LOST"},
    "WON": {"CLOSED"},
    "LOST": {"CLOSED"},
    "CLOSED": set(),
}


@dataclass(frozen=True)
class DisputeEvidence:
    evidence_id: str
    tenant_id: str
    dispute_id: str
    kind: str
    content_hash: str
    captured_at: datetime


def validate_dispute_transition(current: str, target: str) -> None:
    if current not in DISPUTE_STATES or target not in DISPUTE_STATES:
        raise HTTPException(422, "invalid_dispute_state")
    if target not in DISPUTE_TRANSITIONS[current]:
        raise HTTPException(409, "invalid_dispute_transition")


def require_tenant_evidence(evidence: Iterable[DisputeEvidence], tenant_id: str) -> tuple[DisputeEvidence, ...]:
    scoped = tuple(evidence)
    if any(item.tenant_id != tenant_id for item in scoped):
        raise HTTPException(404, "dispute_not_found")
    return scoped


def evidence_timestamp(value: datetime | None = None) -> datetime:
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc)
