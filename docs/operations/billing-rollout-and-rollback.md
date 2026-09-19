# Billing rollout and rollback (Phase 0)

## Scope

Phase 0 ships configuration, startup validation, and documentation only. No
`PaymentAttempt`, provider adapter, ledger, or webhook runtime is introduced.
Nothing in this rollout charges, refunds, transmits, or mutates billing data.

## Rollout

1. Deploy with every `KLYROW_BILLING_*` flag absent or `false` (the shipped
   `.env.example` default). The gateway starts exactly as before; no new
   route or table is added.
2. To exercise Phase 0 validation in a non-production environment, enable
   `KLYROW_BILLING_CORE_ENABLED=true` alone. No dependent capability is
   required; the gateway starts and no provider is contacted.
3. Do not enable `KLYROW_BILLING_STRIPE_ENABLED`, `KLYROW_BILLING_PAYPAL_ENABLED`,
   `KLYROW_BILLING_STABLECOIN_ENABLED`, `KLYROW_BILLING_PROVIDER_WEBHOOKS_ENABLED`,
   `KLYROW_BILLING_DUNNING_ENABLED`, or `KLYROW_BILLING_LIVE_CHARGING_ENABLED`
   in any environment during Phase 0: no adapter exists to serve them, and
   Phase 0 intentionally has no code path that would use a validated secret
   reference for anything beyond startup validation.
4. Production activation remains impossible: every provider/live-charging
   flag requires an explicit secret-reference file that is never generated,
   distributed, or approved by Phase 0.

## Rollback

1. Revert this change (or set every `KLYROW_BILLING_*` flag back to `false`
   / unset).
2. No data migration, secret rotation, or provider deactivation is required:
   Phase 0 never activated a provider, so there is nothing external to tear
   down.
3. Confirm rollback with `pytest -q tests/test_billing_activation_config.py`
   and `python -m compileall -q apps/gateway`.

## Verification checklist

- [ ] `pytest -q tests/test_billing_activation_config.py` passes.
- [ ] `python scripts/export-api-contracts.py --check` passes (no new routes;
      `docs/security/secret-references.json` reflects the new
      `KLYROW_BILLING_*_SECRET_FILE` reference names).
- [ ] Gateway starts with the default (fully disabled) `.env.example` values.
- [ ] Gateway startup fails with a redacted, actionable error when a
      dependent flag is enabled without its prerequisite (see
      `docs/evidence/billing-phase-0-traceability.md`).
