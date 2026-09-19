# Versioned business events and bounded tracing

The mission audit found a legacy event shape, missing daily business snapshots,
no OTLP exporter and incomplete account views. Preserve the existing mail
protocol and introduce normalized business facts alongside it. The reviewed
usage-history and readiness-workflow changes from PRs #130 and #132 are included
in this branch.

`business_events.py` writes immutable facts to the existing transactional
integration outbox. Closed-day totals come from the tenant-scoped usage ledger.
A tenant lock serializes revisions; unchanged replay returns the same fact.
Late ledger entries create a new total with a later `snapshot_at`. Downstream
daily projections must replace older snapshots, never sum the totals or charge
again. No email address, content or provider secret enters these summaries.

`business_event_worker.py` leases outbox rows, retries failures with bounded
backoff, fences completion and retains exhausted work in the existing DLQ.
Only an explicit JSON `202 ACCEPTED` response with an operation ID completes
publication. Redirects and HTML responses retain the fact for retry. Middleware
owns the corresponding authenticated, tenant-bound durable inbox and all future
Odoo projection work. Its inbox acknowledgment does not mean Odoo was updated.

The new AsyncAPI streams coexist with the shared SDK's existing CloudEvents.
JSON Schema versions are immutable. CI validates examples, compiles generated
API types and rejects incompatible changes to published streams/schemas.

OTLP/HTTP tracing uses a bounded asynchronous exporter and explicit attributes.
W3C context is stored with email commands and restored at both Postal delivery
paths. Registered route templates bound metric cardinality. Instrumentation
failures do not alter application results. Tenant identifiers, email addresses,
URLs, SQL and message bodies are not exported as metric labels. Trace setup
follows the [OpenTelemetry Python exporter interface](https://opentelemetry.io/docs/languages/python/exporters/).

## Deployment order

1. Apply Middleware migration `0061_codestra_business_events` and deploy its
   private integration API. Provision the Keycloak service client
   `klyrow-business-events` with audience/issuer/environment bound to that API,
   `klyrow.events.write`, and either its tenant claim or the explicitly scoped
   `klyrow.events.write:any-tenant` permission for this multi-tenant publisher.
2. Complete the private authenticated replay test. Render short-lived JWTs and
   TLS material from OpenBao. Klyrow receives no Odoo credential.
3. Apply Klyrow migration `2026091302_m07_business_event_runtime.sql` (which follows
   `2026091301_outbox_trace_context.sql`) before starting
   the new image. The column is additive and defaults to `{}` for old writers.
4. Apply `deploy/docker-compose.business-events.yml` with the private endpoint,
   token file, TLS directory and `business-events` profile. It exposes no host
   port and keeps existing delivery activation controls disabled.
5. Schedule `python -m app.business_usage --day YYYY-MM-DD` inside the business
   worker image once per closed UTC day. Re-run dates to capture late ledger
   records. Transactions are committed per bounded tenant page.
6. Configure `KLYROW_OTLP_TRACES_ENDPOINT` with the private HTTPS `/v1/traces`
   collector endpoint in the API and workers; optional sampling defaults to
   0.1. Use standard OTLP certificate environment configuration for the private
   CA/mTLS connection. Verify a stored-command trace in the deployed collector.

## Rollback and remaining acceptance

Stop the new publisher and snapshot schedule before reverting either service.
Remove the optional overlay to stop new normalized lifecycle production. Keep
both outbox and inbox records, and keep the additive trace column. Old writers
remain compatible. Return Alertmanager callers to the retained route before
removing its new alias. Do not drop accepted facts during rollback.

This changes source and deployable configuration, not production activation.
The full M05/M06 target still requires additional typed APIs and behavior;
downstream daily Odoo projection, private runtime smoke tests, trace export,
outage recovery, backup restore and protected release evidence remain gates.
`validate-production-acceptance.py` checks release identity, artifact hashes,
freshness and all required M33 results only after trusted evidence signature
verification. It cannot certify arbitrary assertions or enable delivery.
