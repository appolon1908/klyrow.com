# PAS-71 — Durable Journey and Worker Storage

## Authority

This change implements **storage only** for PAS-71 / DB-09–10.

- PostgreSQL is authoritative for accepted durable work.
- PAS-35 owns distributed worker processes, broker/Redis coordination and horizontal-runtime deployment.
- PAS-36 owns Journey execution semantics and state-machine behavior.
- PAS-218 owns the browser BFF/UI/certification layer.
- Existing campaign dispatch storage remains authoritative for campaigns; this change does not replace it.

No provider, Middleware, broker, Redis, Caddy or Kong runtime is changed by this mission.

## Tables

Generic worker persistence:

- `worker_jobs`
- `worker_heartbeats`
- `dead_letters`

Journey execution persistence:

- `journey_node_executions`
- `journey_wakeups`
- `journey_event_waits`
- `journey_goal_hits`

## Invariants

- Logical worker jobs are unique by tenant/job type/aggregate/generation.
- Optional idempotency keys are tenant-scoped.
- Ready jobs use PostgreSQL `FOR UPDATE SKIP LOCKED` claims.
- Leases can be reclaimed after expiry without losing accepted work.
- Lease ownership is verified before completion/retry.
- Dead-letter creation is idempotent per worker job.
- Journey node execution is unique by run/node/execution generation.
- Journey node effects receive a deterministic idempotency key.
- One wakeup and one event wait exist per node execution.
- Goal hits are unique by run/goal/event.
- No in-memory timer is authoritative.

## Migration

`migrations/2026092301_durable_journey_worker_storage.sql`

The migration is additive and uses `CREATE TABLE/INDEX IF NOT EXISTS`. Older application images ignore the new tables.

## Rollback

Rollback means deploy the previous application image and stop PAS-35/PAS-36 workers that depend on these tables. Preserve the tables and rows so accepted work/evidence is not destroyed. A later governed cleanup migration may remove unused tables only after reader/writer inventory proves they are no longer authoritative.

## Follow-up

1. PAS-35: consume `worker_jobs` / `worker_heartbeats` from separate worker roles and prove worker-kill recovery.
2. PAS-36: consume the Journey tables for node execution, waits, event matching, goals, bounded retry and reconciliation.
3. PAS-218: expose the completed durable engine through the frozen browser/public contract and professional UI.
