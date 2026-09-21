# M6D Billing Reconciliation Design

## Authority

`billing_reconciliation.reconcile_billing()` is read-only. It compares provider-event evidence, PaymentAttempt lifecycle, canonical Payment rows, Refund rows, and invoice balances. It does not settle, transition, repair, or rewrite billing history.

## Operator API

The planned endpoint is `GET /v1/internal/billing/reconciliation` with:

- `platform_admin` authorization.
- Optional `tenant_id` filtering.
- Deterministic `PASS` or `DRIFT` output.
- Stable issue codes and resource identifiers.

The router exists in `apps/gateway/app/billing_reconciliation.py` but registration is deferred until the final shared API-contract regeneration checkpoint. This avoids changing global generated inventories while sibling M6 lanes are still active.

## Correlation rules

Provider evidence is checked against the exact PaymentAttempt tenant, invoice, currency, and minor-unit amount. Missing attempts, missing invoices, stale processing leases, duplicate payment-attempt links, and invoice balance/status drift are reported, never silently repaired.

## Tenant isolation

Billing tenant tables are covered by `2026092101_billing_rls_runtime_roles.sql`. The runtime role is non-owner and `NOBYPASSRLS`; policies use `app.tenant_id`. Cross-tenant reconciliation requires the privileged operator path and remains read-only.

## Certification boundary

Real PostgreSQL RLS/concurrency, backup/restore, Postman, staging checkout/readback, and production activation evidence remain environment-backed gates. Live charging remains disabled.
