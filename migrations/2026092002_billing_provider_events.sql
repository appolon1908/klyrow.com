CREATE TABLE IF NOT EXISTS klyrow_billing_provider_events (
 id VARCHAR PRIMARY KEY, provider VARCHAR NOT NULL, provider_event_id VARCHAR NOT NULL,
 event_type VARCHAR NOT NULL, tenant_id VARCHAR NULL, payment_attempt_id VARCHAR NULL,
 invoice_id VARCHAR NULL, livemode BOOLEAN NOT NULL DEFAULT FALSE, api_version VARCHAR NULL,
 payload_json TEXT NOT NULL, payload_hash VARCHAR NOT NULL, processing_state VARCHAR NOT NULL DEFAULT 'RECEIVED',
 attempt_count INTEGER NOT NULL DEFAULT 0, claimed_at TIMESTAMPTZ NULL, claimed_by VARCHAR NULL,
 processed_at TIMESTAMPTZ NULL, next_retry_at TIMESTAMPTZ NULL, last_error_code VARCHAR NULL,
 last_error_message VARCHAR NULL, received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 CONSTRAINT uq_klyrow_billing_provider_event UNIQUE (provider, provider_event_id),
 CONSTRAINT ck_klyrow_billing_provider_event_state CHECK (processing_state IN ('RECEIVED','PROCESSING','PROCESSED','RETRY','IGNORED','DEAD_LETTER'))
);
CREATE INDEX IF NOT EXISTS ix_klyrow_billing_provider_event_claim ON klyrow_billing_provider_events (processing_state, next_retry_at, received_at);