# Codestra observability and Odoo integration

Klyrow accepts bounded Alertmanager events and vetted KPI snapshots, persists
the exact canonical Middleware projection, and relays it with a dedicated
worker. Klyrow never connects to Odoo. Middleware is the only cross-system
writer and owns the durable Odoo delivery record.

The cross-repository contract is vendored at
[`monitoring/kyyow-observability-sync-v1.json`](../monitoring/kyyow-observability-sync-v1.json).
It is pinned to Git blob
`993d89a9649a85d50ef431fa9d8e30fff7c660f3` from Middleware commit
`61d899f98048f4465303cdcb616a4c9f5a35ceb5` at
`contracts/observability/odoo-sync.v1.json`.
The validation script recomputes the Git blob identifier so contract drift
fails CI. The same workflow pins Middleware's active consumer implementation
at Git blob `8748512f71d3856785abb5113e056eb975102421`, including its canonical
UTF-8 projection-hash behavior.

## Runtime ownership

| Component | Responsibility | May write Odoo? |
| --- | --- | --- |
| Codestra-Prometheus | Recording rules and technical alert evaluation | No |
| Codestra-Alertmanager | Deduplicate and send firing/resolved events to Klyrow's private ingress | No |
| Klyrow gateway | Validate, normalize and transactionally enqueue canonical projections | No |
| Klyrow observability worker | Relay the unchanged projection to Middleware using OIDC and mTLS | No |
| Middleware | Authenticate, deduplicate, project and enqueue durable Odoo delivery | Yes; sole writer |
| Odoo | Store `kyyow.observability.incident` and `kyyow.observability.kpi.snapshot` records | Receives only through Middleware |
| Grafana, Loki, Tempo, Alloy, exporters and Superset | Read, collect or visualize telemetry | No |
| Codestra-OpenBao | Secret-reference authority | No |

The delivery path is:

1. The private Klyrow route validates a service bearer with
   `klyrow.observability.write`.
2. The gateway creates one `IntegrationOutbox` row with target
   `MIDDLEWARE_OBSERVABILITY` and one of the canonical Middleware operations.
3. The dedicated `observability` worker claims rows with a PostgreSQL
   `FOR UPDATE SKIP LOCKED` lease.
4. The worker obtains a short-lived Keycloak service token and calls the
   canonical Middleware endpoint over mTLS.
5. Only Middleware's structured `accepted` response completes the Klyrow
   row. Timeouts and 5xx/429 responses retry; permanent failures and exhausted
   retries remain `DEAD_LETTER`.
6. Middleware creates its own `odoo_result_delivery` record and invokes the
   canonical Odoo operation. Middleware acceptance is not represented as an
   Odoo write success in Klyrow.

## Canonical mapping

| Klyrow input | Stored operation | Middleware endpoint | Odoo operation |
| --- | --- | --- | --- |
| Alertmanager firing/resolved event | `observability.incidents.upsert` | `POST /v1/observability/incidents` | `odoo.observability.incidents.upsert` |
| Prometheus recording-rule snapshot | `observability.kpis.create` | `POST /v1/observability/kpis` | `odoo.observability.kpis.create` |

Klyrow stores the complete `kyyow.observability.incident.v1` or
`kyyow.observability.kpi.v1` envelope. It does not emit the removed
`KlyrowObservabilityAlertV1` / `KlyrowKpiSnapshotV1` formats and does not
include an `odoo_model` hint. Incident IDs remain stable by tenant and
Alertmanager fingerprint; resource versions increase across state changes.

## Private Klyrow API

| Method | Path | Permission | Purpose |
| --- | --- | --- | --- |
| POST | `/v1/internal/integrations/alertmanager/events` | `klyrow.observability.write` | Validate and enqueue canonical incident states |
| POST | `/v1/internal/integrations/kpis/snapshots` | `klyrow.observability.write` | Validate vetted recording-rule snapshots and enqueue canonical KPI projections |
| GET | `/v1/internal/integrations/odoo/health` | `klyrow.observability.read` | Report relay configuration, activation and tenant backlog |
| GET | `/v1/internal/integrations/odoo/checkpoints` | `klyrow.observability.read` | Report tenant-scoped operation/state checkpoints without payloads |
| GET | `/v1/internal/integrations/observability/contract` | `klyrow.observability.read` | Report runtime ownership and canonical operations |

There is no observability-specific fake reconciliation event. Authorized
operators recover a failed durable operation through
`POST /v1/operations/{operation_id}/reconcile`, which applies the same
target-specific permission checks as other operations.

Labels are allowlisted and bounded. Tenant, customer, recipient, email, phone,
trace, request, URL and secret-bearing dimensions fail closed. HTTP metrics use
registered route templates instead of raw request paths.

## Deployment and activation

The relay is an optional Compose overlay and remains disabled by default:

```bash
docker compose \
  -f docker-compose.yml \
  -f deploy/docker-compose.observability.yml \
  --profile observability config
```

Before setting `KLYROW_OBSERVABILITY_DELIVERY_ENABLED=true`, operators must
verify:

- immutable image and release digests and a rollback target;
- the pinned contract blob and the private Middleware origin;
- a tenant-bound Keycloak client whose `services` claim contains every
  projected service ID, audience `middleware-api`, and only
  `observability.kpis.write` and `observability.incidents.write`;
- each allowed KPI `query_ref` and each incident `source_deployment`
  registered under the corresponding Middleware service's
  `observation_sources` for that client and environment;
- the root-owned client-secret file and the approved CA/client certificate/key;
- Middleware's Odoo registry routes and Odoo credentials;
- firing, resolution, replay, retry, dead-letter and KPI read-back evidence;
- backup/restore and rollback rehearsal.

This activation is independent of all email-delivery gates and does not enable
live mail.

Validate the boundary with:

```bash
python scripts/validate-codestra-integration.py
python -m pytest -q \
  tests/test_observability_integration.py \
  tests/test_contract_conformance.py
```
