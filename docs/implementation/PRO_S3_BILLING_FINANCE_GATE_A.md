# PRO-S3.A Billing & Finance — Gate A contract freeze

Linear: PAS-213  
Parent: PAS-201  
Baseline: protected `main@ca54d5567d719d7aa09098dca1d5e85c3bd76903`  
Date: 2026-09-22

## Decision

PRO-S3 does **not** create a new billing ledger, provider adapter, payment worker, queue,
audit store, entitlement engine, or settlement authority.

The suite is a composition/product-completion lane over the billing authorities already
present on protected main plus the reviewed M6 provider lanes. PAS-214 may extend browser
read models and professional UI only where this document marks EXTEND/NEW. Cross-system
provider effects remain owned by the M6/shared Middleware integration lanes.

Canonical cross-system path:

```text
Client / Browser -> Caddy -> Kong -> Middleware -> authorized adapter -> service/provider
```

No PAS-214 change may add a second provider path around that contract.

## 1. Current-state inventory

### Protected main

The current protected-main billing baseline is
`ca54d5567d719d7aa09098dca1d5e85c3bd76903`.

Already merged before this freeze:

- PAS-173 / PR #161: single billing runtime/contract composition authority.
- M6D reconciliation/RLS guardrails.
- M6A Stripe live-charge fail-closed gates.
- M6B subscription lifecycle, entitlements, proration, trials and dunning.
- M6C source for refunds, disputes, tax and billing documents on main.

### Open billing source stack

These remain separate provider/persistence lanes and are **not** reimplemented by PRO-S3:

- PR #162 / PAS-170 successor — tax/dispute/receipt persistence and RLS.
- PR #163 / PAS-174 — PayPal provider authority.
- PR #164 / PAS-175 — USDC settlement authority.
- PAS-176 — final billing integration/release seal.
- PAS-190 — hosted GitHub Actions execution blocker.

### Existing Klyrow billing models

Financial/product authority already exists in `apps/gateway/app/billing.py`:

- `klyrow_products`
- `klyrow_plans`
- `klyrow_prices`
- `klyrow_subscriptions`
- `klyrow_usage_events`
- `klyrow_invoices`
- `klyrow_invoice_lines`
- `klyrow_payment_method_references`
- `klyrow_payments`
- `klyrow_credits`
- `klyrow_refunds`
- `klyrow_wallets`
- `klyrow_wallet_transactions`
- `klyrow_tax_rules`
- `klyrow_billing_events`
- `klyrow_billing_work_items`
- `klyrow_checkout_sessions`
- `klyrow_credit_notes`

Payment-attempt authority already exists in `apps/gateway/app/payment_attempts.py`:

- `klyrow_payment_attempts`
- `klyrow_payment_attempt_events`
- one tenant-scoped idempotency key authority
- one state machine
- one provider-adapter protocol boundary

### Existing browser billing BFF

`apps/gateway/app/billing_browser.py` already exposes:

Read:

- `GET /app/api/billing/overview`
- `GET /app/api/billing/subscription`
- `GET /app/api/billing/catalog`
- `GET /app/api/billing/entitlements`
- `GET /app/api/billing/invoices`
- `GET /app/api/billing/invoices/{invoice_id}`
- `GET /app/api/billing/payments`
- `GET /app/api/billing/refunds`
- `GET /app/api/billing/payment-methods`
- `GET /app/api/billing/wallet`
- `GET /app/api/billing/capabilities`
- `GET /app/api/billing/payment-attempts/{payment_attempt_id}`

Browser lifecycle actions:

- `POST /app/api/billing/subscription/quote`
- `POST /app/api/billing/subscription/change`
- `POST /app/api/billing/subscription/cancel`
- `POST /app/api/billing/subscription/reactivate`
- `POST /app/api/billing/invoices/{invoice_id}/checkout`

Browser mutations require the existing browser-session/CSRF and billing-manage authority.
No capture/authorize provider verbs are exposed on the browser BFF.

### Existing portal UI

Implemented portal routes:

- Billing Overview
- Subscription
- Invoices
- Invoice Detail
- Payments
- Refunds
- Payment Methods
- Wallet

Partial portal routes:

- Plan
- Usage

The existing `BillingPortalPage.vue` already renders explicit loading, forbidden,
unavailable/degraded-compatible, error, empty and ready states, and does not display raw
payment credentials.

### Existing API/contract artifacts

Reuse and regenerate the current authorities; do not create another contract inventory:

- `docs/api/current-api.md`
- `docs/api/runtime-routes.json`
- `docs/api/source-handlers.json`
- `schemas/openapi/klyrow-browser-api.yaml`
- generated frontend API types
- canonical Postman generation/certification tracked by PAS-48/PAS-51

## 2. Authority map

| Surface/fact | Classification | Owner | PAS-214 rule |
| --- | --- | --- | --- |
| Product/catalog/pricing | REUSE | `billing.py` Klyrow product/plan/price tables | Read/compose only; no second catalog |
| Financial subscription state | REUSE | `klyrow_subscriptions` + `billing_entitlements.py` | Extend UX/BFF only |
| Entitlements | REUSE | provider-neutral entitlement calculator + billing subscription state | No parallel entitlement engine |
| Usage | EXTEND | `klyrow_usage_events` | Add professional browser read model only |
| Invoice financial truth | REUSE | `klyrow_invoices` + `billing_ledger.py` | Never calculate a second balance |
| Invoice/credit documents | REUSE | existing M6C document authority | Compose/link existing document routes |
| Payment settlement | REUSE | `billing_ledger.post_settlement` + payment-attempt/provider lanes | No new settlement implementation |
| Refund state | REUSE | `klyrow_refunds` + M6C/M6E provider lanes | Browser history/read first |
| Tax/credits | REUSE | M6C billing tables/rules | No new tax engine |
| Wallet | REUSE | `klyrow_wallets` + transactions | Browser read model only unless separately governed |
| Payment methods | REUSE | opaque `PaymentMethodReference` only | Never store/display PAN/CVV |
| Provider capability status | REUSE/EXTEND | `/app/api/billing/capabilities` | May enrich safe capability fields |
| Plan page | EXTEND | existing catalog/subscription BFF | Make current partial route complete |
| Usage page | EXTEND | existing usage/entitlement authority | Make current partial route complete |
| Platform Billing admin view | REUSE/BLOCKED BY OWNER | PAS-197 / PRO-S7 Administration | Consume PAS-197 read model; do not duplicate |
| PayPal | EXTERNAL/REUSE | PAS-174 | No provider duplication |
| Stablecoin/USDC | EXTERNAL/REUSE | PAS-175 | No provider duplication |
| Final billing release seal | EXTERNAL | PAS-176 | PRO-S3 cannot self-certify live charging |
| Shared Middleware/Kong/Caddy integration | EXTERNAL | PAS-229/PAS-230 | No shared integration changes in PAS-214 |
| Shared telemetry/SLO contract | EXTERNAL | PAS-231/PAS-232 | Reuse bounded names/redaction once frozen |

### Historical duplicate-looking models

The repository also contains legacy/general SaaS `plans` and `subscriptions` tables.
They are **not** a reason to create a third billing model.

Freeze:

- `klyrow_plans` = billing product catalog/pricing plan authority.
- `klyrow_subscriptions` = financial subscription authority.
- any legacy/general entitlement-facing plan/subscription representation is a separate
  compatibility/projection boundary and must not be silently merged or replaced by PRO-S3.
- PAS-214 may expose a projection/mapping; it may not create another table family.

## 3. Domain, state and failure model

### Subscription state

Reuse `billing_entitlements.SubscriptionState` and its legal transition matrix:

- TRIALING
- ACTIVE
- PAST_DUE
- GRACE_PERIOD
- CANCEL_AT_PERIOD_END
- SUSPENDED
- CANCELLED
- CLOSED

Rules:

- version is optimistic-concurrency authority;
- browser lifecycle mutation must fail with conflict on stale expected version;
- invalid transitions fail closed;
- plan change reuses the existing proration authority;
- subscription mutation emits the existing durable billing/subscription event path.

### Payment attempt state

Reuse the existing payment-attempt state machine:

- CREATED
- PENDING
- REQUIRES_ACTION
- AUTHORIZED
- CAPTURED
- FAILED
- CANCELLED
- EXPIRED

Rules:

- tenant + scoped idempotency key is unique;
- terminal-state replay does not create a second settlement;
- provider-reference conflicts fail closed;
- confirmed ledger posting is atomic against the tenant invoice;
- browser does not directly capture/authorize a provider.

### Invoice/ledger invariants

- `billing_ledger.invoice_balance` is the single financial balance calculation.
- confirmed payments/refunds determine derived financial state.
- tenant id is mandatory for invoice/payment/refund lookup.
- paid/void/credited invoices cannot be settled again.
- provider evidence is not canonical financial truth by itself.
- invoice creation/mutations that support idempotency must preserve the existing keys.

### Permissions

Read:

- `billing.read`

Browser lifecycle management:

- `billing.manage`
- authenticated browser session
- CSRF on browser mutations

Platform-wide admin read model:

- platform-owner/admin authority from PAS-197, not tenant billing permissions.

### Failure/degraded behavior

The product must represent, rather than hide:

- billing disabled;
- provider unavailable;
- provider sandbox/live mode;
- checkout unavailable;
- stale subscription version;
- invalid lifecycle transition;
- provider reconciliation drift;
- dependency unavailable;
- forbidden/tenant mismatch;
- empty billing history;
- request/correlation id when returned by shared error envelope.

A provider outage must not fabricate a paid invoice, refund, entitlement or wallet mutation.

## 4. API and event contract freeze

### Browser BFF audience

Audience: authenticated same-origin browser only.

All tenant id/organization scope is derived server-side from the authenticated context.
The browser does not submit an arbitrary tenant id to select billing records.

List-like responses use bounded pagination where already present. New PAS-214 list
extensions must use the same bounded-pagination convention and stable safe error codes.

### Allowed PAS-214 API changes

PAS-214 may:

1. add/finish a professional **Plan** browser read projection from existing catalog +
   current subscription authority;
2. add/finish a professional **Usage** browser read projection from existing
   `klyrow_usage_events` + entitlement limits;
3. compose existing invoice document/credit-note document links into the browser UI;
4. enrich safe provider/capability status;
5. consume the PAS-197 Platform Billing admin read model once that owner lane is available;
6. add typed request/response schemas and pagination to those read models;
7. regenerate existing OpenAPI/Postman/frontend contract artifacts.

PAS-214 may **not**:

- add direct Stripe/PayPal/USDC capture/settlement calls;
- add a second webhook authority;
- add a second payment worker/outbox/queue;
- create a new billing ledger or balance calculation;
- create a second subscription state machine;
- create a second entitlement service;
- create duplicate admin billing APIs already owned by PAS-197;
- bypass Middleware for new cross-system effects.

### Event ownership

Reuse existing billing/subscription event and payment/provider durable-event authorities.
New UI/read-model work may subscribe/project existing events but may not invent a competing
financial settlement event stream.

Correlation propagation for any new cross-system call follows PAS-229/PAS-230. Shared
telemetry naming, redaction and SLO thresholds follow PAS-231/PAS-232.

## 5. UX/design contract freeze

### Information architecture

Customer Billing:

1. Overview
2. Plan
3. Subscription
4. Usage
5. Invoices
6. Invoice detail/documents
7. Payments
8. Refunds
9. Payment methods
10. Wallet

Platform Billing:

- owned by PAS-197 Administration & Operations; PRO-S3 links/consumes that surface and does
  not create a parallel admin console.

### Required states

Every Billing page delivered by PAS-214 must have:

- loading
- empty
- ready
- degraded/unavailable
- forbidden
- error/retry

For lifecycle/checkout actions also include:

- submitting/loading
- success/readback
- conflict/stale version
- provider unavailable
- safe retry/idempotent replay messaging

### Accessibility/responsive requirements

- keyboard-reachable actions and filters;
- associated labels for form controls;
- visible focus;
- semantic status/alert regions;
- tables usable on narrow/mobile layouts;
- no color-only status meaning;
- no PAN, CVV, provider secret, signing key or raw provider credential in UI/logs.

## 6. PAS-214 exact implementation handoff

### Source work that remains

1. Complete **Plan** route/page from existing catalog/current subscription data.
2. Complete **Usage** route/page with bounded tenant-scoped usage history/aggregation and
   entitlement-limit context.
3. Wire existing billing document/credit-note document capabilities into the professional
   invoice history/detail experience where not already surfaced.
4. Keep payment/refund/wallet/provider history read-first.
5. Reuse PAS-197 Platform Billing read model instead of implementing a second admin billing
   backend. If PAS-197 is not merged, mark that dependency explicitly rather than copying it.
6. Add/extend backend and frontend tests for authorization, tenant isolation, pagination,
   lifecycle conflict, degraded/provider failure, no-sensitive-data and accessibility states.
7. Regenerate canonical API/OpenAPI/frontend/Postman artifacts.
8. Run focused + affected regression. Hosted CI remains separately blocked by PAS-190 until
   runner execution is restored.

### Shared-gate dependencies

PAS-214 source work inside Klyrow may proceed from this freeze, but the suite must not claim
shared gateway/edge/observability/runtime certification until the corresponding owners
provide evidence:

- PAS-229 — cross-system architecture/contract freeze
- PAS-230 — shared Middleware/Kong/Caddy implementation/certification
- PAS-231 — observability test/SLO freeze
- PAS-232 — monitoring runtime certification
- PAS-190 — hosted GitHub Actions execution
- PAS-176 — final billing production release seal

## 7. Gate A completion record

DONE_SOURCE_INVENTORY=YES  
DONE_AUTHORITY_MAP=YES  
DONE_DOMAIN_STATE_FAILURE_MODEL=YES  
DONE_API_EVENT_FREEZE=YES  
DONE_UX_DESIGN_FREEZE=YES  
DUPLICATE_LEDGER_CREATED=NO  
DUPLICATE_PROVIDER_CREATED=NO  
DUPLICATE_ADMIN_BILLING_CREATED=NO  
SHARED_GATEWAY_RUNTIME_CERTIFIED=NO  
SHARED_OBSERVABILITY_RUNTIME_CERTIFIED=NO  
PRODUCTION_BILLING_GO=NO  

NEXT_TASK=PAS-214 — complete only the frozen composition/UI/read-model gaps above.
