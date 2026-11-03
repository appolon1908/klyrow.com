-- M1-07: random per-session CSRF token stored (encrypted) on the session row.
-- Replaces the deterministic HMAC(SECRET, session_id) derivation; rotation
-- copies the parent's token so the SPA's cached value keeps working.
BEGIN;

ALTER TABLE browser_sessions ADD COLUMN IF NOT EXISTS csrf_ciphertext TEXT;

COMMIT;
