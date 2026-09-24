"""Durable Journey execution state owned by PAS-71.

This module intentionally contains persistence and claim primitives only.
Journey semantics/execution are implemented by PAS-36 on top of these tables.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import CheckConstraint, DateTime, Integer, String, Text, UniqueConstraint, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from .main import Base


NODE_STATES = (
    "PENDING", "READY", "RUNNING", "WAITING", "COMPLETED",
    "SKIPPED", "RETRY", "FAILED", "CANCELLED",
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JourneyNodeExecution(Base):
    __tablename__ = "journey_node_executions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    journey_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    journey_version: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    profile_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    node_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    node_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    execution_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING", index=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    output_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    idempotency_key: Mapped[str] = mapped_column(String(320), nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    lease_owner: Mapped[Optional[str]] = mapped_column(String(200), nullable=True, index=True)
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code_safe: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint(
            "run_id", "node_id", "execution_generation",
            name="uq_journey_node_execution_generation",
        ),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_journey_node_idempotency"),
        CheckConstraint(
            "state IN ('PENDING','READY','RUNNING','WAITING','COMPLETED','SKIPPED','RETRY','FAILED','CANCELLED')",
            name="ck_journey_node_execution_state",
        ),
        CheckConstraint("attempt >= 0", name="ck_journey_node_execution_attempt"),
        CheckConstraint("journey_version >= 1", name="ck_journey_node_execution_version"),
        CheckConstraint("execution_generation >= 0", name="ck_journey_node_execution_generation"),
    )


class JourneyWakeup(Base):
    __tablename__ = "journey_wakeups"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    node_execution_id: Mapped[str] = mapped_column(String(200), nullable=False, unique=True, index=True)
    wake_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="WAITING", index=True)
    claimed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        CheckConstraint(
            "state IN ('WAITING','CLAIMED','COMPLETED','CANCELLED')",
            name="ck_journey_wakeup_state",
        ),
    )


class JourneyEventWait(Base):
    __tablename__ = "journey_event_waits"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    profile_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    node_execution_id: Mapped[str] = mapped_column(String(200), nullable=False, unique=True, index=True)
    event_name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    filter_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="WAITING", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    matched_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "state IN ('WAITING','MATCHED','EXPIRED','CANCELLED')",
            name="ck_journey_event_wait_state",
        ),
    )


class JourneyGoalHit(Base):
    __tablename__ = "journey_goal_hits"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    goal_node_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    event_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    hit_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint(
            "run_id", "goal_node_id", "event_id",
            name="uq_journey_goal_hit",
        ),
    )


def _json(value: object) -> str:
    if isinstance(value, str):
        json.loads(value)
        return value
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def ensure_node_execution(
    session: Session,
    *,
    tenant_id: str,
    journey_id: str,
    journey_version: int,
    run_id: str,
    profile_id: str,
    node_id: str,
    node_type: str,
    execution_generation: int = 0,
    input_payload: object = None,
    scheduled_at: Optional[datetime] = None,
) -> JourneyNodeExecution:
    existing = session.scalar(select(JourneyNodeExecution).where(
        JourneyNodeExecution.run_id == run_id,
        JourneyNodeExecution.node_id == node_id,
        JourneyNodeExecution.execution_generation == execution_generation,
    ))
    if existing is not None:
        return existing
    key = f"journey:{run_id}:{node_id}:{execution_generation}"
    item = JourneyNodeExecution(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        journey_id=journey_id,
        journey_version=journey_version,
        run_id=run_id,
        profile_id=profile_id,
        node_id=node_id,
        node_type=node_type,
        execution_generation=execution_generation,
        input_json=_json({} if input_payload is None else input_payload),
        idempotency_key=key,
        scheduled_at=scheduled_at or utcnow(),
    )
    session.add(item)
    return item


def schedule_wakeup(
    session: Session,
    *,
    tenant_id: str,
    run_id: str,
    node_execution_id: str,
    wake_at: datetime,
) -> JourneyWakeup:
    existing = session.scalar(select(JourneyWakeup).where(
        JourneyWakeup.node_execution_id == node_execution_id,
    ))
    if existing is not None:
        if existing.wake_at != wake_at:
            raise ValueError("journey_wakeup_conflict")
        return existing
    item = JourneyWakeup(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        run_id=run_id,
        node_execution_id=node_execution_id,
        wake_at=wake_at,
    )
    session.add(item)
    return item


def claim_due_wakeup(session: Session, *, at: Optional[datetime] = None) -> Optional[JourneyWakeup]:
    at = at or utcnow()
    item = session.scalar(
        select(JourneyWakeup).where(
            JourneyWakeup.state == "WAITING",
            JourneyWakeup.wake_at <= at,
        ).order_by(JourneyWakeup.wake_at, JourneyWakeup.id)
        .with_for_update(skip_locked=True).limit(1)
    )
    if item is not None:
        item.state = "CLAIMED"
        item.claimed_at = at
    return item


def register_event_wait(
    session: Session,
    *,
    tenant_id: str,
    run_id: str,
    profile_id: str,
    node_execution_id: str,
    event_name: str,
    filter_payload: object = None,
    expires_at: Optional[datetime] = None,
) -> JourneyEventWait:
    existing = session.scalar(select(JourneyEventWait).where(
        JourneyEventWait.node_execution_id == node_execution_id,
    ))
    if existing is not None:
        return existing
    item = JourneyEventWait(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        run_id=run_id,
        profile_id=profile_id,
        node_execution_id=node_execution_id,
        event_name=event_name,
        filter_json=_json({} if filter_payload is None else filter_payload),
        expires_at=expires_at,
    )
    session.add(item)
    return item


def record_goal_hit(
    session: Session,
    *,
    tenant_id: str,
    run_id: str,
    goal_node_id: str,
    event_id: str,
    hit_at: Optional[datetime] = None,
) -> JourneyGoalHit:
    existing = session.scalar(select(JourneyGoalHit).where(
        JourneyGoalHit.run_id == run_id,
        JourneyGoalHit.goal_node_id == goal_node_id,
        JourneyGoalHit.event_id == event_id,
    ))
    if existing is not None:
        return existing
    item = JourneyGoalHit(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        run_id=run_id,
        goal_node_id=goal_node_id,
        event_id=event_id,
        hit_at=hit_at or utcnow(),
    )
    session.add(item)
    return item
