# ADR-006: Billing ownership and provider boundary

Status: accepted, 2026-09-19 (Phase 0 — configuration and documentation only).

## Context

Klyrow will eventually charge tenants through external payment providers
(Stripe, PayPal, stablecoin/crypto rails). Before any provider adapter,
ledger, or webhook runtime exists, the repository needs an explicit,
enforced boundary between Klyrow-owned billing state and provider
infrastructure, plus fail-closed activation controls so no execution path
can accidentally start.

## Decision

- Klyrow owns its internal billing domain and canonical billing state:
  subscriptions, plans, prices, invoices, usage, wallet balances, and
  entitlements recorded in `apps/gateway/app/billing.py` remain the single
  source of truth for what a tenant owes and is entitled to.
- Provider adapters (Stripe, PayPal, stablecoin/crypto) are infrastructure
  boundaries. They never own Klyrow's canonical subscription, invoice,
  payment, or entitlement state; they only execute payment instructions and
  report outcomes back through opaque references.
- External provider identifiers (customer IDs, payment intents, charge IDs,
  transaction hashes) are stored and treated as opaque references. Klyrow
  never infers domain meaning from their internal structure.
- Provider credentials never enter domain records. Only secret *references*
  (file paths resolved outside the request path) are configured; see
  `apps/gateway/app/billing_activation.py`.
- Phase boundaries cannot be combined into a single change:
  - **Phase 0** (this ADR): fail-closed configuration, flags, secret-reference
    validation, startup integration, and documentation only. No
    `PaymentAttempt`, no provider adapter, no ledger, no webhook runtime.
  - **Phase 1**: `PaymentAttempt` domain model and provider adapters (Stripe,
    PayPal, stablecoin) begin executing payment instructions.
  - **Phase 2**: immutable double-entry accounting (ledger accounts, journal
    entries/lines).
  - **Phase 3**: durable webhook ingestion and delivery (both payment-provider
    webhook consumption and customer-facing webhook delivery).
- Stripe, PayPal, and stablecoin adapters cannot start implementation before
  Phases 0–3 are independently merged, in order.

## Consequences

- Phase 0 introduces no new billing routes, tables, or migrations. It adds
  fail-closed flags/secret-reference validation and gateway startup
  enforcement so that enabling a dependent capability without its
  prerequisites blocks startup with a safe, actionable error.
- Future PaymentAttempt/ledger/webhook work must be proposed as separate
  ADRs and phases, each independently reviewable and mergeable.
- See `docs/architecture/billing-target-architecture.md` for the full target
  shape and `docs/adr/ADR-007-billing-ledger-and-webhook-boundaries.md` for the
  ledger/webhook-specific boundary decisions.

## Rollback

Revert this ADR and `apps/gateway/app/billing_activation.py`'s startup hook.
No runtime data, secret, or deployed configuration changes; every flag
defaults to disabled, so rollback does not require deactivating anything in
production.
