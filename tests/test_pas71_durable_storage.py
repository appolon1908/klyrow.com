from datetime import datetime, timedelta, timezone
from pathlib import Path
import inspect

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from apps.gateway.app.durable_jobs import (
    DeadLetter,
    JobLeaseLost,
    WorkerHeartbeat,
    WorkerJob,
    claim_job,
    complete_job,
    dead_letter_job,
    enqueue_job,
    heartbeat_worker,
    recover_expired_leases,
    retry_job,
)
from apps.gateway.app.journey_storage import (
    JourneyEventWait,
    JourneyGoalHit,
    JourneyNodeExecution,
    JourneyWakeup,
    claim_due_wakeup,
    ensure_node_execution,
    record_goal_hit,
    register_event_wait,
    schedule_wakeup,
)


@pytest.fixture
def store():
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    for model in (
        WorkerJob,
        WorkerHeartbeat,
        DeadLetter,
        JourneyNodeExecution,
        JourneyWakeup,
        JourneyEventWait,
        JourneyGoalHit,
    ):
        model.__table__.create(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    yield factory
    engine.dispose()


def test_worker_job_is_idempotent_by_logical_generation_and_key(store):
    with store() as session:
        first = enqueue_job(
            session,
            tenant_id="tenant-a",
            job_type="journey.node",
            aggregate_id="run-a",
            generation=1,
            payload={"node": "start"},
            idempotency_key="job-key-a",
        )
        session.flush()
        same_logical = enqueue_job(
            session,
            tenant_id="tenant-a",
            job_type="journey.node",
            aggregate_id="run-a",
            generation=1,
            payload={"node": "changed-but-not-rewritten"},
        )
        same_key = enqueue_job(
            session,
            tenant_id="tenant-a",
            job_type="other",
            aggregate_id="different",
            idempotency_key="job-key-a",
        )
        next_generation = enqueue_job(
            session,
            tenant_id="tenant-a",
            job_type="journey.node",
            aggregate_id="run-a",
            generation=2,
        )
        assert same_logical.id == first.id
        assert same_key.id == first.id
        assert next_generation.id != first.id
        assert first.payload_json == '{"node":"start"}'


def test_job_lease_recovery_retry_completion_and_lease_guard(store):
    now = datetime.now(timezone.utc)
    with store() as session:
        enqueue_job(
            session,
            tenant_id="tenant-a",
            job_type="journey.node",
            aggregate_id="run-a",
            priority=10,
            available_at=now - timedelta(seconds=1),
        )
        enqueue_job(
            session,
            tenant_id="tenant-a",
            job_type="billing.reconcile",
            aggregate_id="bill-a",
            priority=1,
            available_at=now - timedelta(seconds=1),
        )
        session.commit()

        claimed = claim_job(
            session,
            worker_id="journey-worker-1",
            job_types=("journey.node",),
            at=now,
            lease_seconds=30,
        )
        assert claimed is not None
        assert claimed.job_type == "journey.node"
        assert claimed.state == "LEASED"
        assert claimed.attempts == 1

        claimed.lease_expires_at = now - timedelta(seconds=1)
        session.commit()
        recovered = recover_expired_leases(session, at=now, max_attempts=3)
        assert [item.id for item in recovered] == [claimed.id]
        assert claimed.state == "RETRYING"

        claimed_again = claim_job(
            session,
            worker_id="journey-worker-2",
            job_types=("journey.node",),
            at=now,
        )
        assert claimed_again.id == claimed.id
        assert claimed_again.attempts == 2
        with pytest.raises(JobLeaseLost):
            complete_job(session, claimed_again, worker_id="journey-worker-1")
        claimed_again.lease_expires_at = now - timedelta(microseconds=1)
        with pytest.raises(JobLeaseLost):
            complete_job(session, claimed_again, worker_id="journey-worker-2", at=now)
        claimed_again.lease_expires_at = now + timedelta(seconds=30)
        complete_job(session, claimed_again, worker_id="journey-worker-2", at=now)
        assert claimed_again.state == "COMPLETED"
        assert claimed_again.lease_owner is None


def test_retry_and_dead_letter_are_bounded_and_idempotent(store):
    now = datetime.now(timezone.utc)
    with store() as session:
        job = enqueue_job(
            session,
            tenant_id="tenant-a",
            job_type="journey.action",
            aggregate_id="run-a",
            available_at=now,
        )
        session.commit()
        job = claim_job(session, worker_id="worker-a", at=now)
        retry_job(
            session,
            job,
            worker_id="worker-a",
            delay_seconds=15,
            error_code_safe="dependency_unavailable",
            at=now,
        )
        assert job.state == "RETRYING"
        job.available_at = now
        job = claim_job(session, worker_id="worker-a", at=now)
        first = dead_letter_job(
            session,
            job,
            reason_code="attempts_exhausted",
            last_error_safe="safe detail",
            at=now,
        )
        session.flush()
        second = dead_letter_job(
            session,
            job,
            reason_code="ignored-second-reason",
            at=now,
        )
        assert first.id == second.id
        assert job.state == "DEAD_LETTER"
        assert session.scalar(select(DeadLetter).where(DeadLetter.job_id == job.id)).reason_code == "attempts_exhausted"


def test_worker_heartbeat_updates_one_durable_identity(store):
    t1 = datetime.now(timezone.utc)
    t2 = t1 + timedelta(seconds=5)
    with store() as session:
        first = heartbeat_worker(
            session,
            worker_id="worker-a",
            worker_type="journey",
            instance_id="instance-a",
            version="1",
            release_sha="sha-a",
            at=t1,
        )
        session.flush()
        second = heartbeat_worker(
            session,
            worker_id="worker-a",
            worker_type="journey",
            instance_id="instance-a",
            version="2",
            release_sha="sha-b",
            current_job_id="job-a",
            at=t2,
        )
        assert first is second
        assert second.started_at == t1
        assert second.heartbeat_at == t2
        assert second.release_sha == "sha-b"
        assert second.current_job_id == "job-a"


def test_journey_node_wait_event_and_goal_records_are_idempotent(store):
    wake_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    with store() as session:
        node = ensure_node_execution(
            session,
            tenant_id="tenant-a",
            journey_id="journey-a",
            journey_version=3,
            run_id="run-a",
            profile_id="profile-a",
            node_id="wait-a",
            node_type="WAIT_DURATION",
            input_payload={"minutes": 5},
        )
        session.flush()
        again = ensure_node_execution(
            session,
            tenant_id="tenant-a",
            journey_id="journey-a",
            journey_version=3,
            run_id="run-a",
            profile_id="profile-a",
            node_id="wait-a",
            node_type="WAIT_DURATION",
        )
        assert again.id == node.id

        wakeup = schedule_wakeup(
            session,
            tenant_id="tenant-a",
            run_id="run-a",
            node_execution_id=node.id,
            wake_at=wake_at,
        )
        assert schedule_wakeup(
            session,
            tenant_id="tenant-a",
            run_id="run-a",
            node_execution_id=node.id,
            wake_at=wake_at,
        ).id == wakeup.id
        with pytest.raises(ValueError, match="journey_wakeup_conflict"):
            schedule_wakeup(
                session,
                tenant_id="tenant-a",
                run_id="run-a",
                node_execution_id=node.id,
                wake_at=wake_at + timedelta(minutes=1),
            )

        wait = register_event_wait(
            session,
            tenant_id="tenant-a",
            run_id="run-a",
            profile_id="profile-a",
            node_execution_id=node.id,
            event_name="purchase.completed",
            filter_payload={"sku": "A"},
        )
        assert register_event_wait(
            session,
            tenant_id="tenant-a",
            run_id="run-a",
            profile_id="profile-a",
            node_execution_id=node.id,
            event_name="purchase.completed",
        ).id == wait.id

        hit = record_goal_hit(
            session,
            tenant_id="tenant-a",
            run_id="run-a",
            goal_node_id="goal-a",
            event_id="event-a",
        )
        assert record_goal_hit(
            session,
            tenant_id="tenant-a",
            run_id="run-a",
            goal_node_id="goal-a",
            event_id="event-a",
        ).id == hit.id


def test_due_wakeup_claim_uses_single_claim_state(store):
    now = datetime.now(timezone.utc)
    with store() as session:
        due = JourneyWakeup(
            id="wake-due",
            tenant_id="tenant-a",
            run_id="run-a",
            node_execution_id="exec-a",
            wake_at=now - timedelta(seconds=1),
        )
        future = JourneyWakeup(
            id="wake-future",
            tenant_id="tenant-a",
            run_id="run-b",
            node_execution_id="exec-b",
            wake_at=now + timedelta(hours=1),
        )
        session.add_all([due, future])
        session.commit()
        claimed = claim_due_wakeup(session, at=now)
        assert claimed.id == due.id
        assert claimed.state == "CLAIMED"
        assert claim_due_wakeup(session, at=now) is None


def test_pas71_source_and_migration_freeze_required_storage_contract():
    root = Path(__file__).parents[1]
    sql = (root / "migrations/2026092301_durable_journey_worker_storage.sql").read_text()
    for table in (
        "worker_jobs",
        "worker_heartbeats",
        "dead_letters",
        "journey_node_executions",
        "journey_wakeups",
        "journey_event_waits",
        "journey_goal_hits",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    for invariant in (
        "uq_worker_job_logical_generation",
        "uq_worker_job_idempotency",
        "uq_journey_node_execution_generation",
        "uq_journey_node_idempotency",
        "uq_journey_goal_hit",
        "ix_worker_jobs_ready",
        "ix_worker_jobs_expired_lease",
        "ix_journey_wakeups_due",
        "ix_journey_event_waits_match",
    ):
        assert invariant in sql

    from apps.gateway.app import durable_jobs, journey_storage
    assert "skip_locked=True" in inspect.getsource(durable_jobs.claim_job)
    assert "skip_locked=True" in inspect.getsource(durable_jobs.recover_expired_leases)
    assert "skip_locked=True" in inspect.getsource(journey_storage.claim_due_wakeup)
