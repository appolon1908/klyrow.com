-- PAS-71 / DB-09-10: durable Journey execution and distributed worker storage.
-- PostgreSQL remains the business truth. PAS-35 owns worker deployment/broker
-- topology; PAS-36 owns Journey execution semantics on top of this persistence.
BEGIN;

CREATE TABLE IF NOT EXISTS worker_jobs (
    id VARCHAR NOT NULL PRIMARY KEY,
    tenant_id VARCHAR(200) NOT NULL,
    job_type VARCHAR(100) NOT NULL,
    aggregate_id VARCHAR(200) NOT NULL,
    generation INTEGER NOT NULL DEFAULT 0,
    state VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    priority INTEGER NOT NULL DEFAULT 100,
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    lease_owner VARCHAR(200),
    lease_expires_at TIMESTAMP WITH TIME ZONE,
    payload_json TEXT NOT NULL DEFAULT '{}',
    correlation_id VARCHAR(200),
    idempotency_key VARCHAR(240),
    error_code_safe VARCHAR(160),
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    completed_at TIMESTAMP WITH TIME ZONE,
    CONSTRAINT uq_worker_job_logical_generation
        UNIQUE (tenant_id, job_type, aggregate_id, generation),
    CONSTRAINT uq_worker_job_idempotency UNIQUE (tenant_id, idempotency_key),
    CONSTRAINT ck_worker_job_state CHECK (
        state IN ('PENDING','LEASED','RETRYING','COMPLETED','DEAD_LETTER','CANCELLED')
    ),
    CONSTRAINT ck_worker_job_attempts CHECK (attempts >= 0),
    CONSTRAINT ck_worker_job_priority CHECK (priority >= 0)
);
CREATE INDEX IF NOT EXISTS ix_worker_jobs_tenant_id ON worker_jobs (tenant_id);
CREATE INDEX IF NOT EXISTS ix_worker_jobs_job_type ON worker_jobs (job_type);
CREATE INDEX IF NOT EXISTS ix_worker_jobs_aggregate_id ON worker_jobs (aggregate_id);
CREATE INDEX IF NOT EXISTS ix_worker_jobs_state ON worker_jobs (state);
CREATE INDEX IF NOT EXISTS ix_worker_jobs_priority ON worker_jobs (priority);
CREATE INDEX IF NOT EXISTS ix_worker_jobs_available_at ON worker_jobs (available_at);
CREATE INDEX IF NOT EXISTS ix_worker_jobs_lease_owner ON worker_jobs (lease_owner);
CREATE INDEX IF NOT EXISTS ix_worker_jobs_lease_expires_at ON worker_jobs (lease_expires_at);
CREATE INDEX IF NOT EXISTS ix_worker_jobs_correlation_id ON worker_jobs (correlation_id);
CREATE INDEX IF NOT EXISTS ix_worker_jobs_ready
    ON worker_jobs (priority, available_at, id)
    WHERE state IN ('PENDING','RETRYING');
CREATE INDEX IF NOT EXISTS ix_worker_jobs_expired_lease
    ON worker_jobs (lease_expires_at, id)
    WHERE state = 'LEASED';

CREATE TABLE IF NOT EXISTS worker_heartbeats (
    worker_id VARCHAR(200) NOT NULL PRIMARY KEY,
    worker_type VARCHAR(100) NOT NULL,
    instance_id VARCHAR(200) NOT NULL,
    started_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    heartbeat_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    version VARCHAR(80) NOT NULL,
    release_sha VARCHAR(80) NOT NULL,
    current_job_id VARCHAR(200)
);
CREATE INDEX IF NOT EXISTS ix_worker_heartbeats_worker_type ON worker_heartbeats (worker_type);
CREATE INDEX IF NOT EXISTS ix_worker_heartbeats_instance_id ON worker_heartbeats (instance_id);
CREATE INDEX IF NOT EXISTS ix_worker_heartbeats_heartbeat_at ON worker_heartbeats (heartbeat_at);
CREATE INDEX IF NOT EXISTS ix_worker_heartbeats_current_job_id ON worker_heartbeats (current_job_id);

CREATE TABLE IF NOT EXISTS dead_letters (
    id VARCHAR NOT NULL PRIMARY KEY,
    tenant_id VARCHAR(200) NOT NULL,
    job_id VARCHAR(200) NOT NULL UNIQUE,
    job_type VARCHAR(100) NOT NULL,
    aggregate_id VARCHAR(200) NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    attempts INTEGER NOT NULL DEFAULT 0,
    reason_code VARCHAR(160) NOT NULL,
    last_error_safe VARCHAR(500),
    correlation_id VARCHAR(200),
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_dead_letters_tenant_id ON dead_letters (tenant_id);
CREATE INDEX IF NOT EXISTS ix_dead_letters_job_id ON dead_letters (job_id);
CREATE INDEX IF NOT EXISTS ix_dead_letters_job_type ON dead_letters (job_type);
CREATE INDEX IF NOT EXISTS ix_dead_letters_aggregate_id ON dead_letters (aggregate_id);
CREATE INDEX IF NOT EXISTS ix_dead_letters_correlation_id ON dead_letters (correlation_id);

CREATE TABLE IF NOT EXISTS journey_node_executions (
    id VARCHAR NOT NULL PRIMARY KEY,
    tenant_id VARCHAR(200) NOT NULL,
    journey_id VARCHAR(200) NOT NULL,
    journey_version INTEGER NOT NULL,
    run_id VARCHAR(200) NOT NULL,
    profile_id VARCHAR(200) NOT NULL,
    node_id VARCHAR(200) NOT NULL,
    node_type VARCHAR(80) NOT NULL,
    execution_generation INTEGER NOT NULL DEFAULT 0,
    state VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    attempt INTEGER NOT NULL DEFAULT 0,
    input_json TEXT NOT NULL DEFAULT '{}',
    output_json TEXT NOT NULL DEFAULT '{}',
    idempotency_key VARCHAR(320) NOT NULL,
    scheduled_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    lease_owner VARCHAR(200),
    lease_expires_at TIMESTAMP WITH TIME ZONE,
    started_at TIMESTAMP WITH TIME ZONE,
    completed_at TIMESTAMP WITH TIME ZONE,
    error_code_safe VARCHAR(160),
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    CONSTRAINT uq_journey_node_execution_generation
        UNIQUE (run_id, node_id, execution_generation),
    CONSTRAINT uq_journey_node_idempotency UNIQUE (tenant_id, idempotency_key),
    CONSTRAINT ck_journey_node_execution_state CHECK (
        state IN ('PENDING','READY','RUNNING','WAITING','COMPLETED','SKIPPED','RETRY','FAILED','CANCELLED')
    ),
    CONSTRAINT ck_journey_node_execution_attempt CHECK (attempt >= 0),
    CONSTRAINT ck_journey_node_execution_version CHECK (journey_version >= 1),
    CONSTRAINT ck_journey_node_execution_generation CHECK (execution_generation >= 0)
);
CREATE INDEX IF NOT EXISTS ix_journey_node_executions_tenant_id ON journey_node_executions (tenant_id);
CREATE INDEX IF NOT EXISTS ix_journey_node_executions_journey_id ON journey_node_executions (journey_id);
CREATE INDEX IF NOT EXISTS ix_journey_node_executions_journey_version ON journey_node_executions (journey_version);
CREATE INDEX IF NOT EXISTS ix_journey_node_executions_run_id ON journey_node_executions (run_id);
CREATE INDEX IF NOT EXISTS ix_journey_node_executions_profile_id ON journey_node_executions (profile_id);
CREATE INDEX IF NOT EXISTS ix_journey_node_executions_node_id ON journey_node_executions (node_id);
CREATE INDEX IF NOT EXISTS ix_journey_node_executions_node_type ON journey_node_executions (node_type);
CREATE INDEX IF NOT EXISTS ix_journey_node_executions_state ON journey_node_executions (state);
CREATE INDEX IF NOT EXISTS ix_journey_node_executions_scheduled_at ON journey_node_executions (scheduled_at);
CREATE INDEX IF NOT EXISTS ix_journey_node_executions_lease_owner ON journey_node_executions (lease_owner);
CREATE INDEX IF NOT EXISTS ix_journey_node_executions_lease_expires_at ON journey_node_executions (lease_expires_at);
CREATE INDEX IF NOT EXISTS ix_journey_node_executions_ready
    ON journey_node_executions (scheduled_at, id)
    WHERE state IN ('PENDING','READY','RETRY');
CREATE INDEX IF NOT EXISTS ix_journey_node_executions_expired_lease
    ON journey_node_executions (lease_expires_at, id)
    WHERE state = 'RUNNING';

CREATE TABLE IF NOT EXISTS journey_wakeups (
    id VARCHAR NOT NULL PRIMARY KEY,
    tenant_id VARCHAR(200) NOT NULL,
    run_id VARCHAR(200) NOT NULL,
    node_execution_id VARCHAR(200) NOT NULL UNIQUE,
    wake_at TIMESTAMP WITH TIME ZONE NOT NULL,
    state VARCHAR(20) NOT NULL DEFAULT 'WAITING',
    claimed_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    CONSTRAINT ck_journey_wakeup_state CHECK (
        state IN ('WAITING','CLAIMED','COMPLETED','CANCELLED')
    )
);
CREATE INDEX IF NOT EXISTS ix_journey_wakeups_tenant_id ON journey_wakeups (tenant_id);
CREATE INDEX IF NOT EXISTS ix_journey_wakeups_run_id ON journey_wakeups (run_id);
CREATE INDEX IF NOT EXISTS ix_journey_wakeups_node_execution_id ON journey_wakeups (node_execution_id);
CREATE INDEX IF NOT EXISTS ix_journey_wakeups_wake_at ON journey_wakeups (wake_at);
CREATE INDEX IF NOT EXISTS ix_journey_wakeups_state ON journey_wakeups (state);
CREATE INDEX IF NOT EXISTS ix_journey_wakeups_due
    ON journey_wakeups (wake_at, id) WHERE state = 'WAITING';

CREATE TABLE IF NOT EXISTS journey_event_waits (
    id VARCHAR NOT NULL PRIMARY KEY,
    tenant_id VARCHAR(200) NOT NULL,
    run_id VARCHAR(200) NOT NULL,
    profile_id VARCHAR(200) NOT NULL,
    node_execution_id VARCHAR(200) NOT NULL UNIQUE,
    event_name VARCHAR(120) NOT NULL,
    filter_json TEXT NOT NULL DEFAULT '{}',
    expires_at TIMESTAMP WITH TIME ZONE,
    state VARCHAR(20) NOT NULL DEFAULT 'WAITING',
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    matched_at TIMESTAMP WITH TIME ZONE,
    CONSTRAINT ck_journey_event_wait_state CHECK (
        state IN ('WAITING','MATCHED','EXPIRED','CANCELLED')
    )
);
CREATE INDEX IF NOT EXISTS ix_journey_event_waits_tenant_id ON journey_event_waits (tenant_id);
CREATE INDEX IF NOT EXISTS ix_journey_event_waits_run_id ON journey_event_waits (run_id);
CREATE INDEX IF NOT EXISTS ix_journey_event_waits_profile_id ON journey_event_waits (profile_id);
CREATE INDEX IF NOT EXISTS ix_journey_event_waits_node_execution_id ON journey_event_waits (node_execution_id);
CREATE INDEX IF NOT EXISTS ix_journey_event_waits_event_name ON journey_event_waits (event_name);
CREATE INDEX IF NOT EXISTS ix_journey_event_waits_expires_at ON journey_event_waits (expires_at);
CREATE INDEX IF NOT EXISTS ix_journey_event_waits_state ON journey_event_waits (state);
CREATE INDEX IF NOT EXISTS ix_journey_event_waits_match
    ON journey_event_waits (tenant_id, profile_id, event_name, id)
    WHERE state = 'WAITING';

CREATE TABLE IF NOT EXISTS journey_goal_hits (
    id VARCHAR NOT NULL PRIMARY KEY,
    tenant_id VARCHAR(200) NOT NULL,
    run_id VARCHAR(200) NOT NULL,
    goal_node_id VARCHAR(200) NOT NULL,
    event_id VARCHAR(200) NOT NULL,
    hit_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    CONSTRAINT uq_journey_goal_hit UNIQUE (run_id, goal_node_id, event_id)
);
CREATE INDEX IF NOT EXISTS ix_journey_goal_hits_tenant_id ON journey_goal_hits (tenant_id);
CREATE INDEX IF NOT EXISTS ix_journey_goal_hits_run_id ON journey_goal_hits (run_id);
CREATE INDEX IF NOT EXISTS ix_journey_goal_hits_goal_node_id ON journey_goal_hits (goal_node_id);
CREATE INDEX IF NOT EXISTS ix_journey_goal_hits_event_id ON journey_goal_hits (event_id);

COMMIT;
