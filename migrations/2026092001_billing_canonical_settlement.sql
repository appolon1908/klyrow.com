-- Ordered SQL migration: the runner records applied-file checksums.
ALTER TABLE klyrow_payments
  ADD COLUMN IF NOT EXISTS payment_attempt_id VARCHAR NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_klyrow_payments_payment_attempt
  ON klyrow_payments (payment_attempt_id)
  WHERE payment_attempt_id IS NOT NULL;
ALTER TABLE klyrow_payments
  ADD CONSTRAINT fk_klyrow_payment_attempt_settlement
  FOREIGN KEY (payment_attempt_id) REFERENCES klyrow_payment_attempts(id) NOT VALID;