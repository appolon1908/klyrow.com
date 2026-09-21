BEGIN;

CREATE UNIQUE INDEX IF NOT EXISTS uq_klyrow_active_hosted_checkout_per_invoice
  ON klyrow_payment_attempts (tenant_id, invoice_id)
  WHERE provider IN ('stripe', 'paypal')
    AND status IN ('CREATED', 'PENDING', 'REQUIRES_ACTION', 'AUTHORIZED');

COMMIT;
