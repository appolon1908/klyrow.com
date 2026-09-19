-- Trace transport is metadata only. Existing rows remain valid without it.
ALTER TABLE email_outbox
    ADD COLUMN IF NOT EXISTS trace_context_json TEXT NOT NULL DEFAULT '{}';
