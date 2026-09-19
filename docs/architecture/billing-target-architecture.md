# Billing target architecture

Phase 0 establishes configuration and documentation only. This document
describes the full target shape so later phases build toward a single,
reviewed design instead of drifting.

## Phase overview

| Phase | Scope | Status |
| --- | --- | --- |
| 0 | Fail-closed billing flags, secret-reference validation, startup integration, ADRs, invariants, this document, rollout/rollback procedure, traceability evidence, CI gate | This branch |
| 1 | `PaymentAttempt` domain model; Stripe/PayPal/stablecoin adapters begin executing payment instructions against opaque provider references | Not started |
| 2 | Immutable double-entry accounting: ledger accounts, journal entries/lines, reversing-entry corrections | Not started |
| 3 | Durable webhook ingestion (payment-provider) and delivery (customer-facing), independent of each other | Not started |

Phases 1–3 cannot start implementation before the prior phase is
independently merged (`docs/adr/ADR-006-billing-ownership-and-provider-boundary.md`).

## Component boundary (target)

```mermaid
graph LR
  subgraph Klyrow domain (owned)
    Subscriptions[Subscriptions / Plans / Prices]
    Invoices[Invoices / Credit notes]
    Entitlements[Entitlements]
    PaymentAttempt["PaymentAttempt (Phase 1)"]
    Ledger["Ledger accounts + journals (Phase 2)"]
  end
  subgraph Provider infrastructure (not owned)
    Stripe[Stripe adapter]
    PayPal[PayPal adapter]
    Stablecoin[Stablecoin/crypto adapter]
  end
  subgraph Webhooks (Phase 3)
    ProviderWebhookIngestion[Payment-provider webhook ingestion]
    CustomerWebhookDelivery[Customer webhook delivery]
  end
  Subscriptions --> Invoices --> PaymentAttempt
  PaymentAttempt --> Stripe
  PaymentAttempt --> PayPal
  PaymentAttempt --> Stablecoin
  Stripe --> ProviderWebhookIngestion
  PayPal --> ProviderWebhookIngestion
  Stablecoin --> ProviderWebhookIngestion
  ProviderWebhookIngestion --> PaymentAttempt
  Invoices --> Ledger
  PaymentAttempt --> Ledger
  Invoices --> CustomerWebhookDelivery
```

## Phase 0 activation surface (implemented)

`apps/gateway/app/billing_activation.py` parses and fail-closed validates:

- `KLYROW_BILLING_CORE_ENABLED` — gates every other billing flag.
- `KLYROW_BILLING_ENTITLEMENTS_ENABLED` — requires core.
- `KLYROW_BILLING_STRIPE_ENABLED` / `KLYROW_BILLING_PAYPAL_ENABLED` /
  `KLYROW_BILLING_STABLECOIN_ENABLED` — each requires core and a readable,
  non-empty secret-reference file.
- `KLYROW_BILLING_PROVIDER_WEBHOOKS_ENABLED` — requires at least one enabled
  provider and its webhook verification-secret reference.
- `KLYROW_BILLING_DUNNING_ENABLED` — requires entitlements.
- `KLYROW_BILLING_LIVE_CHARGING_ENABLED` — requires at least one enabled,
  valid provider.

No flag causes a provider network call, charge, refund, or data mutation in
Phase 0. `apps/gateway/app/main.py`'s startup event calls
`validate_billing_activation()` and converts any `BillingActivationError`
into a `RuntimeError`, which prevents the gateway process from starting when
enabled configuration is invalid. Fully disabled configuration (the default)
always starts successfully.
