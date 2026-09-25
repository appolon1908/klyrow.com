BEGIN;

-- Webmail browser/runtime tenant tables receive PostgreSQL RLS defense in depth.
-- Application sessions set app.tenant_id before tenant-scoped access.
DO $klyrow$
DECLARE
  table_name text;
BEGIN
  FOREACH table_name IN ARRAY ARRAY[
    'webmail_mailboxes',
    'webmail_access',
    'webmail_messages',
    'webmail_attachments',
    'inbound_route_configs'
  ] LOOP
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
