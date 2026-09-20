BEGIN;

-- Bind attempts to durable billing records. NOT VALID keeps the migration
-- deployable if a legacy row must be reconciled before final validation.
ALTER TABLE klyrow_payment_attempts
  ADD CONSTRAINT fk_klyrow_payment_attempt_invoice
  FOREIGN KEY (invoice_id) REFERENCES klyrow_invoices(id) NOT VALID;
ALTER TABLE klyrow_payment_attempts
  ADD CONSTRAINT fk_klyrow_payment_attempt_method
  FOREIGN KEY (payment_method_reference_id)
  REFERENCES klyrow_payment_method_references(id) NOT VALID;

-- Provider event identity belongs to the provider account, not one attempt.
ALTER TABLE klyrow_payment_attempt_events ADD COLUMN provider VARCHAR;
ALTER TABLE klyrow_payment_attempt_events ADD COLUMN provider_account_reference VARCHAR;
UPDATE klyrow_payment_attempt_events AS event
SET provider = attempt.provider,
    provider_account_reference = attempt.provider_account_reference
FROM klyrow_payment_attempts AS attempt
WHERE attempt.id = event.payment_attempt_id;
ALTER TABLE klyrow_payment_attempt_events ALTER COLUMN provider SET NOT NULL;
DROP INDEX IF EXISTS uq_klyrow_payment_attempt_events_provider_event;
CREATE UNIQUE INDEX uq_klyrow_payment_attempt_events_provider_event
  ON klyrow_payment_attempt_events (
    provider,
    COALESCE(provider_account_reference, ''),
    provider_event_reference
  )
  WHERE provider_event_reference IS NOT NULL;

COMMIT;
