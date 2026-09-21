BEGIN;

-- Billing tenant tables are isolated for the non-owner runtime role. The
-- bootstrap/worker role may bypass these policies only when explicitly needed
-- for cross-tenant reconciliation, while application sessions set app.tenant_id.
DO $klyrow_role$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'klyrow_runtime') THEN
    CREATE ROLE klyrow_runtime NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
      NOREPLICATION NOBYPASSRLS;
  END IF;
END
$klyrow_role$;

DO $klyrow$
DECLARE
  table_name text;
BEGIN
  FOREACH table_name IN ARRAY ARRAY[
    'klyrow_invoices', 'klyrow_payments', 'klyrow_refunds',
    'klyrow_credits', 'klyrow_payment_attempts',
    'klyrow_payment_attempt_events', 'klyrow_billing_provider_events'
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
