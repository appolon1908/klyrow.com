-- Ordered SQL migration: the runner records applied-file checksums.
-- Defense in depth for the application-level invoice lock.
CREATE UNIQUE INDEX IF NOT EXISTS uq_klyrow_active_stripe_checkout_per_invoice
  ON klyrow_payment_attempts (tenant_id, invoice_id, provider)
  WHERE provider = 'stripe'
    AND status IN ('CREATED', 'PENDING', 'REQUIRES_ACTION', 'AUTHORIZED');
