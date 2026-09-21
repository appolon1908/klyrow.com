# Current Schema Truth

Base: `8d41ec081119335ab995ade15bee781d2902009a` (PR #148 exact head).

The source database inventory is generated at `docs/architecture/database-inventory.json`. At this base, SQLAlchemy metadata contains 136 tables: 122 include a `tenant_id` column and 14 do not. The metadata contains 279 indexes, 54 foreign keys, and 270 constraints. The repository currently contains 45 migration files. Migration files are forward-only numbered SQL and must not be edited after application.

The migration filenames are lexicographically ordered, but four legacy numeric prefixes are duplicated: `004`, `005`, `006`, and `010`. The runner uses the complete filename as the migration version, so this is recorded as a compatibility fact rather than silently renumbered.

API contract validation at this base reports 413 OpenAPI operations:

- Admin: 23
- Browser: 74
- Internal: 50
- Legacy: 1
- Public: 256
- Tracking: 6
- Webhook: 3

Generated route inventories contain 454 runtime routes and 465 source handler records. These numbers are evidence counts, not claims that every route is canonical; Lane 2 classifies compatibility routes separately.

Fresh-database, replay, checksum, and lock validation remain required runtime checks. This document does not mark those checks as passed without PostgreSQL evidence.
