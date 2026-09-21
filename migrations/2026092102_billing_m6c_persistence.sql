BEGIN;

ALTER TABLE klyrow_invoices
  ADD COLUMN IF NOT EXISTS jurisdiction VARCHAR NULL,
  ADD COLUMN IF NOT EXISTS tax_rate NUMERIC(8,6) NULL;

ALTER TABLE klyrow_invoice_lines
  ADD COLUMN IF NOT EXISTS tax_amount NUMERIC(18,2) NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS klyrow_disputes (
    id VARCHAR PRIMARY KEY,
    tenant_id VARCHAR NOT NULL,
    invoice_id VARCHAR NOT NULL,
    payment_id VARCHAR NULL,
    provider_reference VARCHAR NULL,
    category VARCHAR NOT NULL DEFAULT 'GENERAL',
    amount NUMERIC(18,2) NOT NULL DEFAULT 0,
    currency VARCHAR NOT NULL,
    reason VARCHAR NOT NULL,
    status VARCHAR NOT NULL DEFAULT 'OPEN',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at TIMESTAMPTZ NULL
);
CREATE INDEX IF NOT EXISTS ix_klyrow_disputes_tenant_id ON klyrow_disputes(tenant_id);
CREATE INDEX IF NOT EXISTS ix_klyrow_disputes_invoice_id ON klyrow_disputes(invoice_id);
CREATE INDEX IF NOT EXISTS ix_klyrow_disputes_payment_id ON klyrow_disputes(payment_id);

CREATE TABLE IF NOT EXISTS klyrow_invoice_receipts (
    id VARCHAR PRIMARY KEY,
    tenant_id VARCHAR NOT NULL,
    invoice_id VARCHAR NOT NULL,
    payment_id VARCHAR NOT NULL,
    kind VARCHAR NOT NULL DEFAULT 'RECEIPT',
    status VARCHAR NOT NULL DEFAULT 'READY',
    content_type VARCHAR NOT NULL DEFAULT 'application/json',
    checksum VARCHAR NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_klyrow_invoice_receipt_payment UNIQUE (tenant_id, kind, payment_id)
);
CREATE INDEX IF NOT EXISTS ix_klyrow_invoice_receipts_tenant_id ON klyrow_invoice_receipts(tenant_id);
CREATE INDEX IF NOT EXISTS ix_klyrow_invoice_receipts_invoice_id ON klyrow_invoice_receipts(invoice_id);
CREATE INDEX IF NOT EXISTS ix_klyrow_invoice_receipts_payment_id ON klyrow_invoice_receipts(payment_id);

DO $klyrow$
DECLARE
  table_name text;
BEGIN
  FOREACH table_name IN ARRAY ARRAY['klyrow_disputes', 'klyrow_invoice_receipts'] LOOP
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
