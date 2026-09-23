"""Durable generic job storage for distributed Klyrow workers.

PAS-71 owns persistence and lease primitives only. Worker process orchestration,
broker topology, and deployment roles remain PAS-35 responsibilities.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from sqlalchemy import CheckConstraint, DateTime, Integer, String, Text, UniqueConstraint, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from .main import Base


JOB_STATES = ("PENDING", "LEASED", "RETRYING", "COMPLETED", "DEAD_LETTER", "CANCELLED")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobLeaseLost(RuntimeError):
    """Raised when a worker attempts to mutate a job without the active lease."""


class WorkerJob(Base):
    __tablename__ = "worker_jobs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    job_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    aggregate_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING", index=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100, index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    lease_owner: Mapped[Optional[str]] = mapped_column(String(200), nullable=True, index=True)
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    correlation_id: Mapped[Optional[str]] = mapped_column(String(200), nullable=True, index=True)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(240), nullable=True)
    error_code_safe: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "job_type", "aggregate_id", "generation",
            name="uq_worker_job_logical_generation",
        ),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_worker_job_idempotency"),
        CheckConstraint(
            "state IN ('PENDING','LEASED','RETRYING','COMPLETED','DEAD_LETTER','CANCELLED')",
            name="ck_worker_job_state",
        ),
        CheckConstraint("attempts >= 0", name="ck_worker_job_attempts"),
        CheckConstraint("priority >= 0", name="ck_worker_job_priority"),
    )


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"

    worker_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    worker_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    instance_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    version: Mapped[str] = mapped_column(String(80), nullable=False)
    release_sha: Mapped[str] = mapped_column(String(80), nullable=False)
    current_job_id: Mapped[Optional[str]] = mapped_column(String(200), nullable=True, index=True)


class DeadLetter(Base):
    __tablename__ = "dead_letters"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    job_id: Mapped[str] = mapped_column(String(200), nullable=False, unique=True, index=True)
    job_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    aggregate_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reason_code: Mapped[str] = mapped_column(String(160), nullable=False)
    last_error_safe: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    correlation_id: Mapped[Optional[str]] = mapped_column(String(200), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


def _payload(value: object) -> str:
    if isinstance(value, str):
        json.loads(value)
        return value
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def enqueue_job(
    session: Session,
    *,
    tenant_id: str,
    job_type: str,
    aggregate_id: str,
    generation: int = 0,
    payload: object = None,
    priority: int = 100,
    available_at: Optional[datetime] = None,
    correlation_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> WorkerJob:
    """Create one durable logical job, returning an existing idempotent match."""
    if idempotency_key:
        existing = session.scalar(select(WorkerJob).where(
            WorkerJob.tenant_id == tenant_id,
            WorkerJob.idempotency_key == idempotency_key,
        ))
        if existing is not None:
            return existing
    existing = session.scalar(select(WorkerJob).where(
        WorkerJob.tenant_id == tenant_id,
        WorkerJob.job_type == job_type,
        WorkerJob.aggregate_id == aggregate_id,
        WorkerJob.generation == generation,
    ))
    if existing is not None:
        return existing
    item = WorkerJob(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        job_type=job_type,
        aggregate_id=aggregate_id,
        generation=generation,
        payload_json=_payload({} if payload is None else payload),
        priority=priority,
        available_at=available_at or utcnow(),
        correlation_id=correlation_id,
        idempotency_key=idempotency_key,
    )
    session.add(item)
    return item


def recover_expired_leases(
    session: Session,
    *,
    at: Optional[datetime] = None,
    max_attempts: int = 10,
    limit: int = 100,
) -> list[WorkerJob]:
    """Return abandoned leased work to retry or dead-letter state."""
    at = at or utcnow()
    rows = list(session.scalars(
        select(WorkerJob).where(
            WorkerJob.state == "LEASED",
            WorkerJob.lease_expires_at.is_not(None),
            WorkerJob.lease_expires_at < at,
        ).order_by(WorkerJob.lease_expires_at, WorkerJob.id)
        .with_for_update(skip_locked=True).limit(limit)
    ))
    for job in rows:
        job.lease_owner = None
        job.lease_expires_at = None
        job.updated_at = at
        if job.attempts >= max_attempts:
            dead_letter_job(
                session,
                job,
                reason_code="lease_attempts_exhausted",
                last_error_safe=job.error_code_safe,
                at=at,
            )
        else:
            job.state = "RETRYING"
            job.available_at = at
    return rows


def claim_job(
    session: Session,
    *,
    worker_id: str,
    job_types: Optional[Iterable[str]] = None,
    lease_seconds: int = 60,
    at: Optional[datetime] = None,
) -> Optional[WorkerJob]:
    """Claim one ready job with a PostgreSQL SKIP LOCKED lease."""
    at = at or utcnow()
    query = select(WorkerJob).where(
        WorkerJob.state.in_(("PENDING", "RETRYING")),
        WorkerJob.available_at <= at,
    )
    types = tuple(job_types or ())
    if types:
        query = query.where(WorkerJob.job_type.in_(types))
    job = session.scalar(
        query.order_by(WorkerJob.priority, WorkerJob.available_at, WorkerJob.id)
        .with_for_update(skip_locked=True).limit(1)
    )
    if job is None:
        return None
    job.state = "LEASED"
    job.lease_owner = worker_id
    job.lease_expires_at = at + timedelta(seconds=lease_seconds)
    job.attempts += 1
    job.updated_at = at
    return job


def _require_lease(job: WorkerJob, worker_id: str, *, at: Optional[datetime] = None) -> None:
    at = at or utcnow()
    expires = job.lease_expires_at
    if (
        job.state != "LEASED"
        or job.lease_owner != worker_id
        or expires is None
        or expires <= at
    ):
        raise JobLeaseLost("worker_job_lease_lost")


def complete_job(session: Session, job: WorkerJob, *, worker_id: str, at: Optional[datetime] = None) -> WorkerJob:
    at = at or utcnow()
    _require_lease(job, worker_id, at=at)
    job.state = "COMPLETED"
    job.completed_at = at
    job.updated_at = at
    job.lease_owner = None
    job.lease_expires_at = None
    job.error_code_safe = None
    return job


def retry_job(
    session: Session,
    job: WorkerJob,
    *,
    worker_id: str,
    delay_seconds: int,
    error_code_safe: str,
    at: Optional[datetime] = None,
) -> WorkerJob:
    at = at or utcnow()
    _require_lease(job, worker_id, at=at)
    job.state = "RETRYING"
    job.available_at = at + timedelta(seconds=max(0, delay_seconds))
    job.updated_at = at
    job.lease_owner = None
    job.lease_expires_at = None
    job.error_code_safe = error_code_safe[:160]
    return job


def dead_letter_job(
    session: Session,
    job: WorkerJob,
    *,
    reason_code: str,
    last_error_safe: Optional[str] = None,
    at: Optional[datetime] = None,
) -> DeadLetter:
    at = at or utcnow()
    existing = session.scalar(select(DeadLetter).where(DeadLetter.job_id == job.id))
    if existing is not None:
        job.state = "DEAD_LETTER"
        job.lease_owner = None
        job.lease_expires_at = None
        job.updated_at = at
        return existing
    item = DeadLetter(
        id=str(uuid.uuid4()),
        tenant_id=job.tenant_id,
        job_id=job.id,
        job_type=job.job_type,
        aggregate_id=job.aggregate_id,
        payload_json=job.payload_json,
        attempts=job.attempts,
        reason_code=reason_code[:160],
        last_error_safe=(last_error_safe or "")[:500] or None,
        correlation_id=job.correlation_id,
        created_at=at,
    )
    session.add(item)
    job.state = "DEAD_LETTER"
    job.lease_owner = None
    job.lease_expires_at = None
    job.updated_at = at
    return item


def heartbeat_worker(
    session: Session,
    *,
    worker_id: str,
    worker_type: str,
    instance_id: str,
    version: str,
    release_sha: str,
    current_job_id: Optional[str] = None,
    at: Optional[datetime] = None,
) -> WorkerHeartbeat:
    at = at or utcnow()
    item = session.get(WorkerHeartbeat, worker_id)
    if item is None:
        item = WorkerHeartbeat(
            worker_id=worker_id,
            worker_type=worker_type,
            instance_id=instance_id,
            started_at=at,
            version=version,
            release_sha=release_sha,
        )
        session.add(item)
    item.worker_type = worker_type
    item.instance_id = instance_id
    item.heartbeat_at = at
    item.version = version
    item.release_sha = release_sha
    item.current_job_id = current_job_id
    return item
