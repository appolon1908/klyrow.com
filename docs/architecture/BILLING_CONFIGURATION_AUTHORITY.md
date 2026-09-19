# Billing configuration authority (Mission 01)

Status: implemented, fail-closed, disabled by default. No provider SDK,
`PaymentAttempt`, ledger, or webhook runtime is introduced by this mission.

## Purpose

`apps/gateway/app/billing_config.py` is the single, typed authority that
parses and validates every billing-related environment control before any
database engine, worker scheduler, provider client, or billing mutation
exists. It is imported and evaluated at module import time in
`apps/gateway/app/main.py`, strictly before `create_engine(...)` — see
`tests/test_gateway_startup_order.py`, which asserts this ordering both by
source position and by spawning a clean subprocess per configuration case.

## Settings objects

- `BillingSettings` — top-level capability flags plus one `StripeSettings`,
  `PayPalSettings`, and `StablecoinSettings` per provider.
- `StripeSettings` / `PayPalSettings` / `StablecoinSettings` — immutable
  (`@dataclass(frozen=True)`) per-provider configuration. They never hold a
  raw secret value; only `secret_configured: bool` /
  `webhook_secret_configured: bool` facts are retained after validation.
- `BillingCapabilityStatus` — the safe, allowlisted shape returned by
  `GET /v1/billing/capability-status`.

## Flags (all default `false`)

```
KLYROW_BILLING_ENABLED
KLYROW_LIVE_CHARGING_ENABLED
KLYROW_STRIPE_ENABLED
KLYROW_PAYPAL_ENABLED
KLYROW_STABLECOIN_ENABLED
KLYROW_BILLING_WEBHOOK_PROCESSING_ENABLED
KLYROW_BILLING_DUNNING_ENABLED
KLYROW_BILLING_REFUNDS_ENABLED
KLYROW_BILLING_DISPUTE_ACTIONS_ENABLED
KLYROW_BILLING_RECONCILIATION_ENABLED
```

Booleans accept only the documented forms (`1/true/yes/on`,
`0/false/no/off`, case-insensitive); anything else — including an empty
string — fails closed with `invalid_boolean`.

## Validation rules and error codes

| Rule | Error code |
| --- | --- |
| Any dependent flag enabled without `KLYROW_BILLING_ENABLED` | `billing_disabled_dependency` |
| Live charging without an enabled provider | `live_charging_requires_provider` |
| Webhook processing without an enabled provider | `webhook_processing_requires_provider` |
| `KLYROW_{PROVIDER}_ENVIRONMENT` not `sandbox`/`production` | `provider_environment_invalid` |
| Production mode without `KLYROW_{PROVIDER}_PRODUCTION_APPROVED=true` | `production_requires_approval` |
| Production mode with an inline `KLYROW_{PROVIDER}_SECRET` value | `production_inline_secret_rejected` |
| Sandbox mode using a key shaped like a production key (Stripe `sk_live_`/`rk_live_`) | `sandbox_production_key_rejected` |
| Production mode using a key shaped like a sandbox key (Stripe `sk_test_`/`rk_test_`) | `production_sandbox_key_rejected` |
| Sandbox mode targeting a known production endpoint (PayPal `api-m.paypal.com`) | `sandbox_production_endpoint_rejected` |
| Production mode targeting a known sandbox endpoint (PayPal `api-m.sandbox.paypal.com`) | `production_sandbox_endpoint_rejected` |
| Production mode missing a currency allowlist | `production_missing_currency_allowlist` |
| Currency allowlist entry not a 3-letter ISO 4217-shaped code | `currency_allowlist_invalid` |
| Provider enabled with webhook processing/production mode but no `*_WEBHOOK_SECRET_FILE` | (secret-file codes, below) |
| Stablecoin missing chain ID / contract / decimals / confirmation threshold / network allowlist | `stablecoin_missing_chain_id`, `stablecoin_missing_contract`, `stablecoin_invalid_decimals`, `stablecoin_missing_confirmation_threshold`, `stablecoin_missing_network_allowlist` |
| Stablecoin chain ID not present in its own network allowlist | `stablecoin_chain_not_allowlisted` |

### Secret-file safety matrix

`_read_secret_file` enforces, for every `*_SECRET_FILE` /
`*_WEBHOOK_SECRET_FILE` reference:

| Condition | Error code |
| --- | --- |
| Reference env var missing/empty | `secret_reference_missing` |
| Path does not exist / cannot be opened | `secret_file_unavailable` |
| Path is a symlink (checked via `os.lstat`, not followed) | `secret_file_symlink_rejected` |
| Path is not a regular file (directory, FIFO, etc.) | `secret_file_not_regular` |
| File is world-writable (POSIX only — the mode bit is not meaningful on Windows) | `secret_file_world_writable` |
| File exceeds 65536 bytes (checked via a bounded `read(limit + 1)`, never a full read) | `secret_file_oversized` |
| File content is not valid UTF-8 | `secret_file_invalid_encoding` |
| File content is empty/whitespace-only | `secret_file_empty` |

Every raised `BillingConfigError` uses `from None`, so neither the exception
message nor a formatted/chained traceback contains the secret path or value.

## Startup integration

`apps/gateway/app/main.py` calls `load_billing_settings()` immediately after
`SAFE_MODE=safe_mode_enabled()` and before `engine=create_engine(...)`,
`app=FastAPI(...)`, or any `@app.on_event("startup")` hook exists. A
`BillingConfigError` is converted to `RuntimeError(f"billing_config:{code}")`,
which aborts the module import (and therefore the whole process) before any
worker task, database connection, or provider client can be constructed.

`apps/gateway/app/service_worker.py` imports `.main` before scheduling any of
its own loops, so the same gate protects the dedicated worker process.

## Safe capability-status API

`GET /v1/billing/capability-status` (authenticated, reuses the existing
`auth` dependency) returns:

```json
{
  "billing": {
    "enabled": false,
    "live_charging": false,
    "providers": {"stripe": "disabled", "paypal": "disabled", "stablecoin": "disabled"},
    "blocked_reasons": ["billing_disabled"]
  }
}
```

Only the documented boolean/provider-mode/reason-code shape is returned.
Paths, secret values, provider account IDs, and exception strings are never
included; an invalid configuration is reported identically to fully-disabled.

## Out of scope for this mission

No provider SDK, `PaymentAttempt`, ledger/journal, webhook runtime, or live
charging is implemented. See the repository's Phase 0/1/2/3 boundary
documents for the target sequencing.
