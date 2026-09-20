BEGIN;
CREATE TABLE IF NOT EXISTS klyrow_payment_attempts (
 id VARCHAR PRIMARY KEY,
 tenant_id VARCHAR NOT NULL,
 invoice_id VARCHAR NOT NULL,
 customer_id VARCHAR NULL,
 payment_method_reference_id VARCHAR NULL,
 provider VARCHAR NOT NULL,
 provider_account_reference VARCHAR NULL,
 provider_attempt_reference VARCHAR NULL,
 idempotency_key VARCHAR NOT NULL,
 request_fingerprint VARCHAR NOT NULL,
 amount_minor BIGINT NOT NULL CHECK (amount_minor > 0),
 currency VARCHAR NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
 status VARCHAR NOT NULL CHECK (status IN
   ('CREATED','PENDING','REQUIRES_ACTION','AUTHORIZED','CAPTURED','FAILED','CANCELLED','EXPIRED')),
 failure_code VARCHAR NULL,
 failure_message VARCHAR NULL,
 next_action_type VARCHAR NULL,
 next_action_reference VARCHAR NULL,
 correlation_id VARCHAR NULL,
 created_by VARCHAR NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 authorized_at TIMESTAMPTZ NULL,
 captured_at TIMESTAMPTZ NULL,
 failed_at TIMESTAMPTZ NULL,
 cancelled_at TIMESTAMPTZ NULL,
 expires_at TIMESTAMPTZ NULL,
 version INTEGER NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_klyrow_payment_attempts_tenant_idempotency
  ON klyrow_payment_attempts (tenant_id, idempotency_key);
CREATE UNIQUE INDEX IF NOT EXISTS uq_klyrow_payment_attempts_provider_reference
  ON klyrow_payment_attempts (provider, provider_attempt_reference)
  WHERE provider_attempt_reference IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_klyrow_payment_attempts_tenant_id ON klyrow_payment_attempts(tenant_id);
CREATE INDEX IF NOT EXISTS ix_klyrow_payment_attempts_invoice_id ON klyrow_payment_attempts(invoice_id);
CREATE INDEX IF NOT EXISTS ix_klyrow_payment_attempts_status ON klyrow_payment_attempts(status);
CREATE INDEX IF NOT EXISTS ix_klyrow_payment_attempts_created_at ON klyrow_payment_attempts(created_at);
CREATE INDEX IF NOT EXISTS ix_klyrow_payment_attempts_tenant_invoice ON klyrow_payment_attempts(tenant_id, invoice_id);

CREATE TABLE IF NOT EXISTS klyrow_payment_attempt_events (
 id VARCHAR PRIMARY KEY,
 payment_attempt_id VARCHAR NOT NULL REFERENCES klyrow_payment_attempts(id),
 tenant_id VARCHAR NOT NULL,
 from_status VARCHAR NULL,
 to_status VARCHAR NOT NULL,
 event_type VARCHAR NOT NULL,
 source VARCHAR NOT NULL,
 provider_event_reference VARCHAR NULL,
 idempotency_key VARCHAR NULL,
 payload_digest VARCHAR NULL,
 correlation_id VARCHAR NULL,
 created_by VARCHAR NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_klyrow_payment_attempt_events_provider_event
  ON klyrow_payment_attempt_events (payment_attempt_id, provider_event_reference)
  WHERE provider_event_reference IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_klyrow_payment_attempt_events_attempt_id
  ON klyrow_payment_attempt_events(payment_attempt_id);
CREATE INDEX IF NOT EXISTS ix_klyrow_payment_attempt_events_tenant_id
  ON klyrow_payment_attempt_events(tenant_id);
CREATE INDEX IF NOT EXISTS ix_klyrow_payment_attempt_events_created_at
  ON klyrow_payment_attempt_events(created_at);
COMMIT;
