# PaymentAttempt foundation (Mission 02)

Status: provider-neutral foundation only. No provider SDK, live Stripe/PayPal/
stablecoin adapter, webhook receiver, or production activation exists yet.
Billing and every provider remain disabled by default (Mission 01,
`apps/gateway/app/billing_config.py`).

## Lifecycle and state machine

```
CREATED --> PENDING --> REQUIRES_ACTION --> AUTHORIZED --> CAPTURED
   |            |              |                |
   v            v              v                v
CANCELLED    FAILED        CANCELLED         CANCELLED
   |            |              |                |
   v            v              v                v
EXPIRED      CANCELLED      EXPIRED           EXPIRED
                  |
                  v
              AUTHORIZED / CAPTURED / FAILED / EXPIRED
```

| From | To |
| --- | --- |
| `CREATED` | `PENDING`, `CANCELLED`, `EXPIRED` |
| `PENDING` | `REQUIRES_ACTION`, `AUTHORIZED`, `CAPTURED`, `FAILED`, `CANCELLED`, `EXPIRED` |
| `REQUIRES_ACTION` | `PENDING`, `AUTHORIZED`, `CAPTURED`, `FAILED`, `CANCELLED`, `EXPIRED` |
| `AUTHORIZED` | `CAPTURED`, `FAILED`, `CANCELLED`, `EXPIRED` |
| `CAPTURED`, `FAILED`, `CANCELLED`, `EXPIRED` (terminal) | none |

All transitions are centralized in `payment_attempts._apply_transition`, run
inside the caller's existing DB transaction, and append exactly one
`PaymentAttemptEvent`. An identical replay (same target status, or same
`provider_event_reference`) is a no-op returning the current state. An invalid
transition — including any transition attempted from a terminal state —
raises `PaymentAttemptConflict` (`409 invalid_payment_attempt_transition`).

## API endpoints

| Method | Path | Audience |
| --- | --- | --- |
| `POST` | `/v1/billing/payment-attempts` | Authenticated tenant |
| `GET` | `/v1/billing/payment-attempts` | Authenticated tenant (tenant-scoped list) |
| `GET` | `/v1/billing/payment-attempts/{id}` | Authenticated tenant (tenant-scoped) |
| `GET` | `/v1/billing/payment-attempts/{id}/events` | Authenticated tenant (tenant-scoped) |
| `POST` | `/v1/billing/payment-attempts/{id}/cancel` | Authenticated tenant (tenant-scoped) |
| `POST` | `/v1/internal/billing/payment-attempts/{id}/transition` | `platform_admin` only; ordinary tenant users receive `403` |

Every tenant-facing query and mutation filters by `tenant_id` at the database
level (never "fetch all, filter after"); cross-tenant reads/mutations return
`404 payment_attempt_not_found`, matching the repository's existing
`tenant_item` convention in `billing.py`.

## Authorization model

Reuses the existing `auth`/`require` dependencies unchanged. The internal
transition endpoint requires `require("platform_admin")` — no new permission
plumbing was introduced, consistent with existing admin-only billing routes
(`/v1/admin/billing/catalog`, `/v1/admin/billing/dunning`).

## Idempotency

- `Idempotency-Key` header is required (400/422 if missing or malformed) on
  `POST /v1/billing/payment-attempts`.
- The key is bound to the full command identity via the existing
  `scoped_idempotency_key(ctx, raw_key, action=..., resource=...)` helper
  (already used by `middleware_email.py`, `production_api.py`,
  `runtime_authority_fixes.py`) and stored in a `(tenant_id, idempotency_key)`
  unique database constraint.
- The request fingerprint is `semantic_request_hash(...)` over exactly
  `{tenant_id, invoice_id, payment_method_reference_id, provider,
  amount_minor, currency}` — never a secret, token, header, or timestamp.
- Same key + same fingerprint → the original attempt is returned unchanged
  (`IDEMPOTENT_REPLAY` metric increments).
- Same key + different fingerprint → `409 idempotency_key_payload_mismatch`.
- A `IntegrityError` race on the unique constraint (concurrent identical
  creation) is caught and resolved to the same idempotent-replay or conflict
  outcome — exactly one durable attempt is ever created.
- Internal transitions carrying a `provider_event_reference` are deduplicated
  via a `(payment_attempt_id, provider_event_reference)` unique constraint on
  `PaymentAttemptEvent`.

## Amount conversion boundary

Existing `Invoice`/`Payment`/`Credit` rows use `Decimal` amounts quantized to
2 places (`billing.money`). `PaymentAttempt.amount_minor` is an integer
(e.g. cents). `payment_attempts._minor_from_decimal` /
`_decimal_from_minor` are the *only* conversion points. This assumes every
currently supported currency uses 2 minor-unit decimal places; 0- or
3-decimal ISO currencies (e.g. JPY, BHD) are an explicit Mission 03+
deferral, documented at the top of `apps/gateway/app/payment_attempts.py`.

## Invoice invariants enforced before creation

- Invoice exists and belongs to the authenticated tenant (`404` otherwise).
- Invoice status is not `VOID`/`CREDITED` (`409 invoice_not_payable`).
- Currency matches the invoice currency exactly (`422 currency_mismatch`).
- Amount is positive (Pydantic `gt=0`, `422` otherwise).
- Amount does not exceed the *remaining eligible balance* — invoice total
  minus credits minus confirmed legacy `Payment` rows minus already-`CAPTURED`
  `PaymentAttempt` rows for that invoice — so captured attempts can never
  collectively overpay an invoice (`409 amount_exceeds_remaining_balance`).
- Refunds (`billing.Refund`) and wallet transactions are untouched by this
  calculation; they are not payment attempts.

## Provider-disabled behavior (fail-closed)

`enforce_billing_capability` reads the canonical Mission 01
`load_billing_settings()` on every request (never cached, never a network
call):

| Condition | Response |
| --- | --- |
| `KLYROW_BILLING_ENABLED=false` | `503 billing_disabled` |
| Billing enabled, but the requested provider is not `"disabled"` and its own flag is off | `503 provider_disabled` |
| Internal transition targets `AUTHORIZED`/`CAPTURED` for a real provider without `KLYROW_LIVE_CHARGING_ENABLED=true` | `503 live_charging_disabled` |
| Provider not in `{"disabled","stripe","paypal","stablecoin"}` | `422` (schema) / `unsupported_provider` (adapter registry) |
| Any adapter operation actually invoked (`create/confirm/capture/cancel`) | `provider_unavailable` — Mission 02 registers no real adapter for any provider, including enabled ones |

No PaymentAttempt code path makes a network call, contacts Stripe/PayPal/a
stablecoin network, or moves money.

## Explicit Mission 03+ deferrals

Live Stripe/PayPal/stablecoin adapters; provider webhook receiver and
signature verification; provider refunds; disputes/chargebacks; settlement
reconciliation; payment-method collection UI; multi-decimal-place currency
support; production activation; deployment.
