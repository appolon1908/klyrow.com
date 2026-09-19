# ADR-007: Billing ledger and webhook boundaries

Status: accepted, 2026-09-19 (Phase 0 — configuration and documentation only;
no ledger or webhook runtime is implemented by this ADR).

## Context

Phase 0 must document, ahead of implementation, how future accounting and
webhook processing will behave so that fail-closed flags and secret
references introduced now stay compatible with later phases without
rework.

## Ledger boundary (Phase 2)

- Posted ledger records are immutable. No future code path may update or
  delete a posted journal entry or journal line.
- Corrections use reversing entries: a new, linked journal entry that negates
  the original, never an in-place edit.
- Every journal must balance by currency. Multi-currency journals are
  modeled as a set of currency-scoped balanced journals, not a single
  cross-currency balance.
- Money values use integer minor units (or an explicitly reviewed, safe
  decimal policy consistent with the existing `Numeric(18,2)`/`Numeric(18,6)`
  columns in `apps/gateway/app/billing.py`). Currency is always explicit and
  validated against ISO 4217-style three-letter codes, matching the existing
  `Field(pattern=r"^[A-Z]{3}$")` convention in that module.

## Webhook boundary (Phase 3)

- Payment-provider webhook ingestion requires signature verification,
  deduplication, replay protection, durable processing, and safe retry
  behavior before any state mutation is accepted.
- Customer-facing webhook delivery (Klyrow notifying tenant-configured
  endpoints) is a separate system from payment-provider webhook ingestion.
  They do not share credentials, queues, or code paths.
- Provider callbacks are never trusted without verification; an unverified
  callback must not be able to mark an invoice paid, trigger a refund, or
  change entitlement state.

## Phase 0 scope boundary

None of the above is implemented in Phase 0. Phase 0 only:

- Documents these boundaries (this ADR).
- Adds fail-closed flags (`KLYROW_BILLING_PROVIDER_WEBHOOKS_ENABLED`, and the
  per-provider enable flags) that must remain disabled by default.
- Requires webhook verification-secret *references* to be present and valid
  before `KLYROW_BILLING_PROVIDER_WEBHOOKS_ENABLED` can activate, even though
  no webhook receiver exists yet — this prevents a future implementation from
  inheriting an unreviewed, already-enabled flag.

## Rollback

Revert this ADR. No ledger table, webhook route, or provider credential
exists yet, so rollback has no runtime effect.
