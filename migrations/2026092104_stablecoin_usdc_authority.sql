BEGIN;

CREATE TABLE IF NOT EXISTS klyrow_stablecoin_payment_requests (
    id VARCHAR PRIMARY KEY,
    tenant_id VARCHAR NOT NULL,
    invoice_id VARCHAR NOT NULL,
    payment_attempt_id VARCHAR NOT NULL UNIQUE,
    chain_id INTEGER NOT NULL,
    token_contract VARCHAR NOT NULL,
    token_decimals INTEGER NOT NULL,
    recipient_address VARCHAR NOT NULL,
    wallet_reference VARCHAR NOT NULL,
    amount_base_units VARCHAR NOT NULL,
    state VARCHAR NOT NULL DEFAULT 'AWAITING_TRANSFER',
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_klyrow_stablecoin_requests_tenant_id ON klyrow_stablecoin_payment_requests(tenant_id);
CREATE INDEX IF NOT EXISTS ix_klyrow_stablecoin_requests_invoice_id ON klyrow_stablecoin_payment_requests(invoice_id);

CREATE TABLE IF NOT EXISTS klyrow_stablecoin_chain_events (
    id VARCHAR PRIMARY KEY,
    tenant_id VARCHAR NOT NULL,
    invoice_id VARCHAR NOT NULL,
    payment_attempt_id VARCHAR NOT NULL,
    payment_request_id VARCHAR NOT NULL,
    chain_id INTEGER NOT NULL,
    tx_hash VARCHAR NOT NULL,
    log_index INTEGER NULL,
    block_number INTEGER NULL,
    block_hash VARCHAR NULL,
    token_contract VARCHAR NOT NULL,
    recipient_address VARCHAR NOT NULL,
    amount_base_units VARCHAR NOT NULL,
    confirmation_count INTEGER NOT NULL DEFAULT 0,
    state VARCHAR NOT NULL DEFAULT 'PENDING',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    claimed_at TIMESTAMPTZ NULL,
    claimed_by VARCHAR NULL,
    next_retry_at TIMESTAMPTZ NULL,
    last_error_code VARCHAR NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_klyrow_stablecoin_chain_tx UNIQUE (chain_id, tx_hash)
);
CREATE INDEX IF NOT EXISTS ix_klyrow_stablecoin_events_tenant_id ON klyrow_stablecoin_chain_events(tenant_id);
CREATE INDEX IF NOT EXISTS ix_klyrow_stablecoin_events_attempt_id ON klyrow_stablecoin_chain_events(payment_attempt_id);
CREATE INDEX IF NOT EXISTS ix_klyrow_stablecoin_events_state ON klyrow_stablecoin_chain_events(state);

CREATE UNIQUE INDEX IF NOT EXISTS uq_klyrow_active_payment_attempt_per_invoice
  ON klyrow_payment_attempts (tenant_id, invoice_id)
  WHERE provider IN ('stripe', 'paypal', 'stablecoin')
    AND status IN ('CREATED', 'PENDING', 'REQUIRES_ACTION', 'AUTHORIZED');

DO $klyrow$
DECLARE
  table_name text;
BEGIN
  FOREACH table_name IN ARRAY ARRAY[
    'klyrow_stablecoin_payment_requests',
    'klyrow_stablecoin_chain_events'
  ] LOOP
    EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', table_name);
    EXECUTE format('DROP POLICY IF EXISTS %I_tenant_isolation ON public.%I', table_name, table_name);
    EXECUTE format(
      'CREATE POLICY %I_tenant_isolation ON public.%I USING (tenant_id = current_setting(''app.tenant_id'', true)) WITH CHECK (tenant_id = current_setting(''app.tenant_id'', true))',
      table_name, table_name
    );
  END LOOP;
END
$klyrow$;

COMMIT;
