# Database Authority Matrix

This Lane 2 packet freezes source ownership without deleting historical tables. Generated table metadata remains in `docs/architecture/database-inventory.json`; this document records the authority rule used for later lanes.

| Domain | Canonical writer | Readers | Tenant rule | Status |
| --- | --- | --- | --- | --- |
| Contacts and profiles | Klyrow profile/contact application services | Browser BFF, campaigns, imports | Tenant-owned; tenant derived from session | Requires source-level mapping review |
| API keys | Klyrow API-key service | Auth middleware, admin APIs | Tenant-owned except platform keys | Requires source-level mapping review |
| Plans and subscriptions | Billing domain authority | Billing BFF, entitlements | Tenant-owned subscription; global plan catalog | Billing authority in PR #148 |
| Domains | Domain verification service | Campaign preflight, sending policy | Tenant-owned | Requires source-level mapping review |
| Campaigns | Campaign application service | Browser BFF, workers, reporting | Tenant-owned | Lane 4 authority review |
| Business events | Klyrow durable event/outbox service | Workers, integrations, audit | Tenant-scoped or platform-scoped by event | Existing source authority |
| Worker jobs | Durable worker/job service | Worker processes, reconciliation | Tenant binding required for tenant jobs | Lane 5 migration coordination |

No external system is a source-of-truth writer for Klyrow-owned tables. Middleware remains the only Odoo writer. Redis is coordination/cache only, never business truth.

Historical duplicate names are retained pending evidence-based migration planning. No table is retired by this packet.
