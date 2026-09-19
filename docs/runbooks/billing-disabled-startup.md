# Runbook: billing disabled at startup (Mission 01)

## What "disabled" means

With every `KLYROW_BILLING_*` / `KLYROW_STRIPE_*` / `KLYROW_PAYPAL_*` /
`KLYROW_STABLECOIN_*` flag absent or `false` (the shipped `.env.example`
default), the gateway starts exactly as it did before this mission:

- No database connection, worker task, or provider client related to billing
  is created.
- `GET /v1/billing/capability-status` returns
  `{"billing": {"enabled": false, "live_charging": false, "providers": {"stripe": "disabled", "paypal": "disabled", "stablecoin": "disabled"}, "blocked_reasons": ["billing_disabled"]}}`.
- No secret file referenced by a billing env var is opened.

## Diagnosing a startup failure

If the gateway process exits immediately with
`RuntimeError: billing_config:<code>`, the configuration is invalid and fail-closed.
Look up `<code>` in
`docs/architecture/BILLING_CONFIGURATION_AUTHORITY.md`'s rule table. Common
cases:

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `billing_config:billing_disabled_dependency` | A provider/dunning/refunds/etc. flag is `true` while `KLYROW_BILLING_ENABLED` is `false` | Set `KLYROW_BILLING_ENABLED=true`, or disable the dependent flag |
| `billing_config:secret_reference_missing` | A provider is enabled but its `*_SECRET_FILE` is unset | Set the env var to an absolute path to a regular, non-empty, UTF-8 secret file |
| `billing_config:secret_file_unavailable` | The referenced file does not exist or cannot be opened | Verify the path and file permissions |
| `billing_config:production_requires_approval` | `KLYROW_{PROVIDER}_ENVIRONMENT=production` without the matching `*_PRODUCTION_APPROVED=true` | Only set this after the production approval process is complete; do not set it to unblock local development |
| `billing_config:sandbox_production_key_rejected` | A sandbox-mode provider's secret file contains a key shaped like a live key | Use a genuine sandbox/test credential |

## Recovery

Setting `KLYROW_BILLING_ENABLED=false` (and leaving every dependent flag
unset) always restores a startable configuration; no data migration, secret
rotation, or provider deactivation is required, because no provider was ever
contacted.

## Verification

```
pytest -q tests/test_billing_config.py tests/test_gateway_startup_order.py
python -m compileall -q apps/gateway
```
