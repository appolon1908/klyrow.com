# ADR-003: Tenant usage history from the metering ledger

Status: accepted for source implementation, 2026-09-13.

## Decision

Add authenticated `GET /v1/usage/daily` and `/v1/usage/monthly` to the
existing FastAPI composition. Aggregate `klyrow_usage_events` in SQL, scoped to
the authenticated tenant and selected unit. Default to `accepted_message`.
This is metered acceptance, not a count of recipient deliveries. Do not combine
the separate provider event ledger with these records or double-count callback
replays. Preserve existing `/v1/usage` and `/v1/billing/usage` contracts.

`from` is an inclusive UTC date; `to` is exclusive. Daily defaults cover the
last 30 UTC dates including today. Monthly defaults cover the current month
through today and the preceding 11 months. Requests cover at most 366 days.
Boundary months can be partial. Periods without ledger records are omitted.
PostgreSQL grouping explicitly converts to UTC, independently of session timezone.

Cursor pagination operates on aggregated periods, never on a truncated ledger
sample. Cursors retain the original window and bind tenant, unit and granularity.
Every query independently enforces tenant authorization; a cursor is not an
access credential. Results are live totals, not a frozen billing statement:
late ledger entries may change periods already read. Clients needing refreshed
historical totals should restart pagination.

Reject unknown filters, including `project_id`, with 422. The current ledger
has no project ownership column, so a project filter would misrepresent isolation.
Add that filter only with an explicit persistence/backfill contract.

## Consequences and verification

No migration, provider call, Odoo call or telemetry dependency is introduced.
Existing tenant indexes support the bounded query, but very large tenants may
require a measured composite-index or rollup migration. A date bound limits
history, not the number of ledger rows. PostgreSQL reports have a five-second
transaction-local statement timeout; production latency and capacity still require
load evidence. Do not claim this endpoint replaces a warehouse for heavy BI.

Tests cover tenant/unit separation, all ledger rows beyond 500, partial months,
boundaries, cursor continuity, invalid filters, authentication and redacted
store failures. An isolated PostgreSQL test sets a non-UTC session timezone and
checks both granularities; CI includes that test in its PostgreSQL gate.

Rollback is a source revert and redeployment of the previous immutable image.
There is no data rollback. New SDK methods and contracts should be reverted
with the routes if this unreleased slice is rolled back.
