# Current Schema Truth

Base: `8d41ec081119335ab995ade15bee781d2902009a` (PR #148 exact head).

The source database inventory is generated at `docs/architecture/database-inventory.json`. The repository currently contains 45 migration files. Migration files are forward-only numbered SQL and must not be edited after application.

API contract validation at this base reports 403 OpenAPI operations:

- Browser: 69
- Callback: 10
- Internal: 72
- Public: 252

Generated route inventories contain 444 runtime routes and 455 source handler records. These numbers are evidence counts, not claims that every route is canonical; Lane 2 classifies compatibility routes separately.

Fresh-database, replay, checksum, and lock validation remain required runtime checks. This document does not mark those checks as passed without PostgreSQL evidence.
