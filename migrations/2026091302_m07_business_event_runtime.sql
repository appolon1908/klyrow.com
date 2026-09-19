-- M07: Klyrow-owned business-event publication, expiring secret responses,
-- and fenced campaign fan-out. Middleware remains the only downstream writer.
BEGIN;

CREATE TABLE IF NOT EXISTS business_event_outbox (
    id VARCHAR NOT NULL PRIMARY KEY,
    event_id VARCHAR(200) NOT NULL UNIQUE,
    event_type VARCHAR(120) NOT NULL,
    tenant_id VARCHAR(200) NOT NULL,
    aggregate_id VARCHAR(200),
    payload TEXT NOT NULL,
    payload_hash VARCHAR(64) NOT NULL,
    correlation_id VARCHAR(200) NOT NULL,
    trace_context TEXT NOT NULL DEFAULT '{}',
    state VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    lease_owner VARCHAR(200),
    lease_expires_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    delivered_at TIMESTAMP WITH TIME ZONE,
    last_error VARCHAR(500),
    CONSTRAINT uq_business_event_tenant_event UNIQUE (tenant_id, event_id),
    CONSTRAINT ck_business_event_state CHECK (
        state IN ('PENDING','LEASED','RETRYING','DELIVERED','DEAD_LETTER')
    ),
    CONSTRAINT ck_business_event_attempts CHECK (attempt_count >= 0),
    CONSTRAINT ck_business_event_payload_hash CHECK (payload_hash ~ '^[0-9a-f]{64}$')
);
CREATE INDEX IF NOT EXISTS ix_business_event_outbox_event_type ON business_event_outbox (event_type);
CREATE INDEX IF NOT EXISTS ix_business_event_outbox_tenant_id ON business_event_outbox (tenant_id);
CREATE INDEX IF NOT EXISTS ix_business_event_outbox_aggregate_id ON business_event_outbox (aggregate_id);
CREATE INDEX IF NOT EXISTS ix_business_event_outbox_state ON business_event_outbox (state);
CREATE INDEX IF NOT EXISTS ix_business_event_outbox_next_attempt_at ON business_event_outbox (next_attempt_at);
CREATE INDEX IF NOT EXISTS ix_business_event_outbox_publish_ready
    ON business_event_outbox (state, next_attempt_at, created_at)
    WHERE state IN ('PENDING','RETRYING');
CREATE INDEX IF NOT EXISTS ix_business_event_outbox_expired_lease
    ON business_event_outbox (lease_expires_at)
    WHERE state = 'LEASED';

CREATE TABLE IF NOT EXISTS secret_responses (
    id VARCHAR NOT NULL PRIMARY KEY,
    tenant_id VARCHAR(200) NOT NULL,
    resource_type VARCHAR(80) NOT NULL,
    resource_id VARCHAR(200) NOT NULL,
    action VARCHAR(40) NOT NULL,
    encrypted_payload TEXT,
    created_by VARCHAR(200) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    retrieved_at TIMESTAMP WITH TIME ZONE,
    redacted_at TIMESTAMP WITH TIME ZONE,
    CONSTRAINT ck_secret_response_redaction CHECK (
        (encrypted_payload IS NOT NULL AND redacted_at IS NULL)
        OR (encrypted_payload IS NULL AND redacted_at IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS ix_secret_responses_tenant_id ON secret_responses (tenant_id);
CREATE INDEX IF NOT EXISTS ix_secret_responses_resource_id ON secret_responses (resource_id);
CREATE INDEX IF NOT EXISTS ix_secret_responses_expires_at ON secret_responses (expires_at);
CREATE INDEX IF NOT EXISTS ix_secret_responses_cleanup
    ON secret_responses (expires_at, id) WHERE encrypted_payload IS NOT NULL;

ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS rotated_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE scoped_api_keys ADD COLUMN IF NOT EXISTS rotated_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE webhook_endpoints ADD COLUMN IF NOT EXISTS rotated_at TIMESTAMP WITH TIME ZONE;

ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS sender_id VARCHAR;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS template_id VARCHAR;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS segment_id VARCHAR;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS current_version INTEGER NOT NULL DEFAULT 1;

CREATE TABLE IF NOT EXISTS campaign_version_snapshots (
    id VARCHAR NOT NULL PRIMARY KEY,
    tenant_id VARCHAR(200) NOT NULL,
    campaign_id VARCHAR(200) NOT NULL,
    campaign_version INTEGER NOT NULL,
    sender_id VARCHAR(200) NOT NULL,
    sender VARCHAR(320) NOT NULL,
    template_id VARCHAR(200) NOT NULL,
    template_version INTEGER NOT NULL,
    subject VARCHAR(998) NOT NULL,
    html_body TEXT NOT NULL,
    text_body TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    CONSTRAINT uq_campaign_version_snapshot UNIQUE (campaign_id, campaign_version)
);
CREATE INDEX IF NOT EXISTS ix_campaign_version_snapshots_tenant_id ON campaign_version_snapshots (tenant_id);
CREATE INDEX IF NOT EXISTS ix_campaign_version_snapshots_campaign_id ON campaign_version_snapshots (campaign_id);

CREATE TABLE IF NOT EXISTS campaign_audience_snapshots (
    id VARCHAR NOT NULL PRIMARY KEY,
    tenant_id VARCHAR(200) NOT NULL,
    campaign_id VARCHAR(200) NOT NULL,
    campaign_version INTEGER NOT NULL,
    recipient VARCHAR(320) NOT NULL,
    recipient_hash VARCHAR(64) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    CONSTRAINT uq_campaign_audience_recipient UNIQUE (campaign_id, campaign_version, recipient_hash),
    CONSTRAINT ck_campaign_audience_hash CHECK (recipient_hash ~ '^[0-9a-f]{64}$')
);
CREATE INDEX IF NOT EXISTS ix_campaign_audience_snapshots_tenant_id ON campaign_audience_snapshots (tenant_id);
CREATE INDEX IF NOT EXISTS ix_campaign_audience_snapshots_campaign_id ON campaign_audience_snapshots (campaign_id);

CREATE TABLE IF NOT EXISTS campaign_dispatch_runs (
    id VARCHAR NOT NULL PRIMARY KEY,
    tenant_id VARCHAR(200) NOT NULL,
    campaign_id VARCHAR(200) NOT NULL UNIQUE,
    campaign_version INTEGER NOT NULL,
    state VARCHAR(20) NOT NULL DEFAULT 'SCHEDULED',
    scheduled_at TIMESTAMP WITH TIME ZONE NOT NULL,
    next_attempt_at TIMESTAMP WITH TIME ZONE NOT NULL,
    lease_owner VARCHAR(200),
    lease_expires_at TIMESTAMP WITH TIME ZONE,
    fence_token INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    completed_at TIMESTAMP WITH TIME ZONE,
    CONSTRAINT ck_campaign_dispatch_run_state CHECK (
        state IN ('SCHEDULED','RUNNING','RETRYING','PAUSED','COMPLETED','CANCELLED','FAILED')
    )
);
CREATE INDEX IF NOT EXISTS ix_campaign_dispatch_runs_tenant_id ON campaign_dispatch_runs (tenant_id);
CREATE INDEX IF NOT EXISTS ix_campaign_dispatch_runs_campaign_id ON campaign_dispatch_runs (campaign_id);
CREATE INDEX IF NOT EXISTS ix_campaign_dispatch_runs_state ON campaign_dispatch_runs (state);
CREATE INDEX IF NOT EXISTS ix_campaign_dispatch_runs_scheduled_at ON campaign_dispatch_runs (scheduled_at);
CREATE INDEX IF NOT EXISTS ix_campaign_dispatch_runs_next_attempt_at ON campaign_dispatch_runs (next_attempt_at);

CREATE TABLE IF NOT EXISTS campaign_dispatch_items (
    id VARCHAR NOT NULL PRIMARY KEY,
    tenant_id VARCHAR(200) NOT NULL,
    campaign_id VARCHAR(200) NOT NULL,
    campaign_version INTEGER NOT NULL,
    audience_id VARCHAR(200) NOT NULL,
    recipient_hash VARCHAR(64) NOT NULL,
    state VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    lease_owner VARCHAR(200),
    fence_token INTEGER,
    message_id VARCHAR(200),
    last_error VARCHAR(300),
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    CONSTRAINT uq_campaign_dispatch_recipient UNIQUE (campaign_id, campaign_version, recipient_hash),
    CONSTRAINT ck_campaign_dispatch_item_state CHECK (
        state IN ('PENDING','LEASED','RETRYING','DELIVERED','SUPPRESSED','FAILED','CANCELLED')
    )
);
CREATE INDEX IF NOT EXISTS ix_campaign_dispatch_items_tenant_id ON campaign_dispatch_items (tenant_id);
CREATE INDEX IF NOT EXISTS ix_campaign_dispatch_items_campaign_id ON campaign_dispatch_items (campaign_id);
CREATE INDEX IF NOT EXISTS ix_campaign_dispatch_items_state ON campaign_dispatch_items (state);
CREATE INDEX IF NOT EXISTS ix_campaign_dispatch_items_ready
    ON campaign_dispatch_items (state, next_attempt_at, created_at)
    WHERE state IN ('PENDING','RETRYING');

COMMIT;
