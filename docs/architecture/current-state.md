# Klyrow repository truth — M00

Inspected baseline: `229a3d88ad159eed8fa55cb4f6312388273f554f` (2026-09-13).
Scope is `appolon1908-hue/klyrow.com`; this is source evidence, not a production certification.

## Runtime and disposition

| Component | Evidence | Disposition | Reason |
| --- | --- | --- | --- |
| Python/FastAPI/SQLAlchemy control plane | `apps/gateway/app/platform.py`, `main.py` | KEEP | Existing composed application, tenant authorization, durable acceptance and tests |
| Vue 3/Vite/TypeScript portal | `apps/web/package.json` | KEEP | Existing browser BFF and OIDC flows; not React/Next.js |
| PostgreSQL application store | `migrations/`, `scripts/migrate` | KEEP | Migration ledger and restricted runtime role |
| SQLite | development/test default in `main.py` | KEEP for tests | Not a production replacement for PostgreSQL concurrency tests |
| Postal + MariaDB + RabbitMQ | `docker-compose.yml` | WRAP | Existing delivery plane; preserve provider normalization and uncertain-outcome reconciliation |
| Mautic + MariaDB | `mautic_adapter.py`, `mautic_contract.py` | WRAP | Governed asynchronous marketing commands |
| SMTP ingress/security worker | `smtp_relay.py`, `security_smtp_worker.py` | KEEP | STARTTLS, hashed credentials, durable queue, encrypted security MIME |
| Gateway composition extensions | `runtime_authority_fixes.py`, `platform.py` | REFACTOR incrementally | Preserve captured helper and route ordering semantics |
| Middleware integration slice | `integrations/codestra-middleware/` | KEEP/EXTEND | Separate deployment authority; pinned upstream dependencies in `SOURCE.json` |
| Inbound-mail Odoo JSON-RPC transport | `integrations/codestra-middleware/app/workers/klyrow_mail_odoo.py` | REPLACE through versioned adapter | Existing `/jsonrpc` contract must remain compatible until Odoo JSON-2/model migration is verified |
| Historical provider proxy examples | `docker/proxy/` | WRAP then retire | Target edge is Caddy → Kong; existing examples use nginx |
| Prometheus/Grafana/node-exporter | `docker-compose.yml`, `config/`, `monitoring/` | WRAP | Connect to central private observability; no business-write authority |
| Local human authentication | `main.py`, `browser_security_fixes.py` | REMOVE from production authority | Keycloak owns production human identity; compatibility tests retain local development paths |

No stack rewrite is justified. Azure Service Bus, Event Hubs, AKS, Redis,
ClickHouse/ADX and Terraform are target options, not implemented dependencies
of this checked-out runtime. No first-party Redis service is declared in the
base Compose file. SQL outboxes and Postal RabbitMQ are the current queues.

## APIs and persistence

The inspected baseline had 378 composed operations, including 29 hidden
operations: OpenAPI documented 349, of which 225 were PUBLIC. After adding the
two template-history reads and two usage-history reads, the generated [API catalog](../api/current-api.md)
contains 382 composed operations, 353 documented operations and 229 PUBLIC
operations. The catalog separately
records browser, admin, internal, tracking and callback audiences.
`docs/api/source-handlers.json` includes declarations from secondary apps and
the Middleware integration slice; declared paths can have router prefixes.
The Postal provisioning service has its own contract at
`openapi/postal-provisioner.openapi.yaml`.

`docs/architecture/database-inventory.json` records the 130 registered tables
and their columns. Tenant-owned data generally uses `tenant_id`, not the new
blueprint's `organization_id`/`project_id`. Organization membership and OIDC
identity links exist. Do not silently rename tenant keys.

The checked-in migrations establish a non-superuser runtime role with
`NOBYPASSRLS`, but no `CREATE POLICY`/`ENABLE ROW LEVEL SECURITY` statements were
found in `migrations/`. Application tenant predicates are the present boundary;
RLS cannot be reported as implemented merely because the role cannot bypass it.

Message admission is asynchronous (`POST /v1/messages` → 202). Durable
idempotency, outbox processing, delivery outcome normalization and tenant
callback attribution have dedicated tests. Health endpoints already include
`/health/live` and `/health/ready`. `/metrics` requires a dedicated file-backed
credential. Infrastructure exposes Postal, Mautic, RabbitMQ and database
protocols on their declared internal networks; these are not customer REST APIs.

## Odoo and secrets

The only included executable Odoo client is the Middleware-owned
`RestrictedOdooTransport`. Its entrypoint is
`app.entrypoints.klyrow_mail_odoo_worker`; it authenticates and calls
`codestra.mail.inbound.event.ingest_event`. It supports leases, bounded retries
and dead letters. It handles intentionally routed inbound business mail, not
daily KPI snapshots. No Odoo SQL connection was found in application code.

Gateway `IntegrationOutbox.target == ODOO` and `odoo_reference` fields are
business routing references, not direct Odoo clients. The gateway sends its
provider/usage events to the authenticated Middleware receiver. The included
receiver stores usage events in `klyrow_usage_event_inbox` and delivery events
in `klyrow_delivery_analytics`. A complete KPI aggregator → Odoo writer →
reconciler loop is not present in this slice. Existing Odoo KPI model names,
field mappings and runtime credentials are not supplied here.

`docs/security/secret-references.json` inventories identifier names and source
locations only. Compose secret declarations reference files under `secrets/`;
gateway production readers require mounted secret files. API keys and SMTP
credentials are hashed. SECURITY MIME and durable operation responses have
encryption/keyring modules. `scripts/generate-env`, `scripts/migrate-runtime-secrets`
and backup scripts manage sensitive files; generated values are never inventory
output. OpenBao workload-auth bindings and ACLs must be verified in their owning
repository and deployment; mounted files alone do not prove OpenBao ownership.

## Observability and delivery gaps

Prometheus metrics, recording rules and alert rules are checked in. Their
current names differ from some target metric names; preserve them while adding
compatible recordings. OpenTelemetry SDK instrumentation/OTLP export is not in
the gateway dependency lock. Central Alloy/Loki/Tempo/Superset configuration is
external. Monitoring onboarding explicitly records `runtime_coverage=unverified`
and `activation_enabled=false`.

The webhook subscription model stores a hash and `secret://webhooks/` reference;
the reference alone is not proof of persisted signing material or a complete
dispatcher. Verify runtime signing, retry and rotation before certifying M25.
Many existing list endpoints and response models still need the M06 typed
response/cursor contract migration. Do not fabricate schemas or promise routes
that do not exist.

## CI and verification

`.github/workflows/ci.yml` runs Python and frontend checks, migration-twice,
PostgreSQL replay tests, a pinned Middleware overlay contract suite, secret and
dependency scans, image scans and SBOM work. Other workflows cover database and
Mautic images, integration contracts and deployment readiness. Backup, restore,
off-host archive and restore verification scripts exist. Executing source tests
does not demonstrate a production restore, firewall denial or successful Odoo
outage recovery.

Run `python scripts/export-api-contracts.py --check` to detect inventory/schema
drift. Run `pytest -q tests` and the pinned Middleware overlay suite for source
verification. Production evidence remains tracked in `PRODUCTION-READINESS.md`.
