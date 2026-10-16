-- Client portal support center: tenant-scoped browser conversation persistence.
-- Reuses canonical support_tickets from operations.py. No provider dispatch is added.
BEGIN;

CREATE TABLE IF NOT EXISTS support_ticket_messages (
    id VARCHAR NOT NULL PRIMARY KEY,
    ticket_id VARCHAR NOT NULL REFERENCES support_tickets(id) ON DELETE CASCADE,
    tenant_id VARCHAR NOT NULL,
    author_user_id VARCHAR NOT NULL,
    author_kind VARCHAR(20) NOT NULL DEFAULT 'CUSTOMER',
    body TEXT NOT NULL,
    idempotency_key VARCHAR(220) NOT NULL,
    request_hash VARCHAR(64) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    CONSTRAINT uq_support_message_tenant_idempotency UNIQUE (tenant_id, idempotency_key),
    CONSTRAINT ck_support_message_author_kind CHECK (
        author_kind IN ('CUSTOMER','SUPPORT','SYSTEM')
    )
);
CREATE INDEX IF NOT EXISTS ix_support_ticket_messages_ticket_id ON support_ticket_messages (ticket_id);
CREATE INDEX IF NOT EXISTS ix_support_ticket_messages_tenant_id ON support_ticket_messages (tenant_id);
CREATE INDEX IF NOT EXISTS ix_support_ticket_messages_author_user_id ON support_ticket_messages (author_user_id);

DO $klyrow$
DECLARE
  table_name text;
BEGIN
  FOREACH table_name IN ARRAY ARRAY['support_tickets', 'support_ticket_messages'] LOOP
    IF to_regclass('public.' || table_name) IS NOT NULL THEN
      EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', table_name);
      EXECUTE format('DROP POLICY IF EXISTS %I_tenant_isolation ON public.%I', table_name, table_name);
      EXECUTE format(
        'CREATE POLICY %I_tenant_isolation ON public.%I USING (tenant_id = current_setting(''app.tenant_id'', true)) WITH CHECK (tenant_id = current_setting(''app.tenant_id'', true))',
        table_name, table_name
      );
    END IF;
  END LOOP;
END
$klyrow$;

COMMIT;
