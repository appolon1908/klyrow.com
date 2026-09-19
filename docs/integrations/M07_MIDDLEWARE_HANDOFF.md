# M07 Middleware handoff contract

Status: Klyrow producer implemented; the paired Middleware M07 source implements the signed ingress, durable inbox, projection outbox, and gated Odoo transport. Exact-head merge and runtime certification remain external gates.

Production remains **NOT AUTHORIZED** by M07. Enabling the publisher requires the Middleware inbox, private-network policy, service identity, and acceptance smoke test described here. The campaign dispatcher is separately gated by `KLYROW_CAMPAIGN_DISPATCHER_ENABLED` and defaults to disabled.

## Ownership boundary

Klyrow is the sole writer of Klyrow email-domain state and its `business_event_outbox`. It creates immutable business summaries in the same PostgreSQL transaction as the originating mutation where applicable, then publishes them asynchronously.

Middleware owns durable receipt, deduplication, retry toward downstream systems, reconciliation, and all external Odoo writes. Klyrow has no Odoo client, credential, ORM model, SQL connection, or network route. Odoo retains authority over its own database and models.

Logs, traces, Prometheus samples, individual opens, and individual clicks are not Odoo business events. Analytics and observability pipelines consume those signals outside this contract.

## Endpoint and transport

| Property | Contract |
|---|---|
| Method | `POST` |
| Path | `/api/v1/events/klyrow` |
| Scheme | HTTPS only; redirects are disabled |
| Request media type | `application/json` |
| Authentication | `Authorization: Bearer <service token>` plus `X-Signature: sha256=<HMAC-SHA256>` from separate OpenBao secret files; production deployment also requires a Klyrow client certificate and trusted internal CA |
| Signed identity headers | `X-Event-Id: <event.id>` and `X-Timestamp: <unix-seconds>` |
| Idempotency header | `Idempotency-Key: <event.id>` |
| Correlation header | `X-Correlation-Id: <event.correlation_id>` |
| Trace headers | Stored W3C `traceparent` and optional `tracestate` from the originating transaction |

The configured URL must be an HTTPS origin with the exact path above, no user-info, query, fragment, redirect following, or non-standard port. Network policy must permit the Klyrow business-event worker to reach only the private Middleware ingress. It must not permit Klyrow to reach Odoo.

The signature canonicalization is byte-for-byte compatible with Middleware's Telnexa ingress pattern:

```text
timestamp + "\\n" + event_id + "\\n" + "klyrow" + "\\n" + raw_json
```

Middleware verifies the Bearer credential, signature, timestamp freshness, required headers, exact header/body identity, media type, and bounded raw body before parsing or accepting an event.

## Event envelope

Every request body is a version-1 envelope:

```json
{
  "id": "evt_opaque-sortable-or-random-id",
  "type": "klyrow.usage.daily",
  "version": 1,
  "source": "klyrow",
  "tenant_id": "tnt_opaque-id",
  "correlation_id": "cor_opaque-id",
  "causation_id": "optional-prior-operation-or-event-id",
  "occurred_at": "2026-09-13T00:00:00Z",
  "data": {}
}
```

Unknown envelope fields, unsupported event types, invalid UTC timestamps, and data that does not match the selected event type are rejected by Klyrow before publication. The canonical schemas are:

- `schemas/json-schema/klyrow-business-event.v1.json`
- `schemas/json-schema/klyrow-*.v1.data.json`
- `schemas/asyncapi/klyrow-business-events-v1.yaml`

`payload_hash` is a Klyrow persistence invariant: lowercase SHA-256 of the canonical JSON request body. It is not an additional envelope field and Middleware must calculate its own inbox hash from the received bytes/canonical document.

## Event types and field schemas

| Type | Required `data` fields | Intended Odoo/business projection |
|---|---|---|
| `klyrow.tenant.created` | `tenant_id`, `enabled`; optional `name`, `organization_id` | Create/link the Codestra tenant projection without changing Klyrow authority |
| `klyrow.tenant.updated` | `tenant_id`, `enabled`; optional `name`, `organization_id` | Update the existing tenant projection |
| `klyrow.subscription.changed` | `subscription_id`, `status`, `version`, `effective_at`; optional `plan_id`, `price_id` | Upsert subscription state only when the event version is newer |
| `klyrow.usage.daily` | `date`, `unit=accepted_message`, `quantity`, `snapshot_at` | Upsert the `(tenant,date,unit)` usage snapshot by newest `snapshot_at`; never add revisions together |
| `klyrow.kpi.daily` | `date`, `accepted`, `delivered`, `bounced`, `complained`, `snapshot_at` | Upsert daily aggregate KPI facts |
| `klyrow.campaign.summary` | `campaign_id`, `campaign_version`, terminal `status`, `audience_count`, `delivered_count`, `suppressed_count`, `failed_count` | Upsert one terminal campaign summary; no per-recipient activity |
| `klyrow.domain.status` | `domain_id`, `domain`, `status`; optional `verified_at` | Update the domain verification/status projection |
| `klyrow.provider.health` | `provider`, `status`, `checked_at`; optional `reason_code` | Update bounded provider-health business status, not raw metrics |
| `klyrow.account.held` | `status=HELD`, `reason`, `changed_at` | Apply an account hold projection and record its reason |
| `klyrow.account.released` | `status=RELEASED`, `reason`, `changed_at` | Release the corresponding account hold projection |

Optional means the JSON property may be absent or `null` as defined by the generated JSON Schema. Middleware must validate against the versioned schema before accepting the operation.

## Acceptance response

Klyrow marks an event delivered only for this response:

```http
HTTP/1.1 202 Accepted
Content-Type: application/json

{"operation_id":"op_<first-32-hex-of-sha256('klyrow\\0' + event.id)>","status":"ACCEPTED"}
```

The body must contain exactly `operation_id` and `status`; `operation_id` must equal the deterministic event-bound value above; `status` must be exactly `ACCEPTED`.

The following never acknowledge the outbox event: 200 (including HTML), malformed or non-JSON 202, 202 with a missing/invalid operation ID, 202 with another status, 301/302, 400, 401, 403, 404, 409, 500, 502, 503, and 504. This contract does **not** grant 409 duplicate-acceptance semantics. A future version may add that only with an explicit, tested Middleware guarantee.

## Retry and dead-letter semantics

Klyrow retries network errors, timeouts, and HTTP 408, 429, 500, 502, 503, and 504. It uses capped exponential backoff with jitter and honors a valid delta-seconds or HTTP-date `Retry-After` value. The defaults are eight attempts, a two-second base, a 900-second cap, a five-second request timeout, and a 60-second lease. All are bounded configuration values.

Authorization, schema, routing, redirect, and malformed-acceptance responses are permanent failures and enter `DEAD_LETTER` immediately. Exhausted transient failures also enter `DEAD_LETTER`. Payload and error code remain durable; response bodies and credentials are never retained as errors.

An authorized replay resets a hash-verified dead-letter row to `RETRYING` with the same event ID and immutable payload. Replay endpoint:

```text
POST /v1/admin/operations/business-events/{event_id}/replay
```

## Lease, fencing, and restart behavior

Publisher states are `PENDING`, `LEASED`, `RETRYING`, `DELIVERED`, and `DEAD_LETTER`. A claim writes a unique `lease_owner`, expiry, and incremented attempt counter in one locked transaction. Completion is conditional on row ID, state, owner, attempt, and lease expiry. A stale process cannot acknowledge or reschedule another worker's lease.

After a crash, an expired lease returns to `RETRYING`, or to `DEAD_LETTER` when attempts are exhausted. Publication uses the unchanged event ID as its idempotency key, so an ambiguous accepted request is safe to send again.

## Middleware inbox and deduplication

Before returning 202, Middleware must durably commit an inbox/operation record containing at least:

```text
operation_id
source = klyrow
event_id
tenant_id
event_type
payload_hash
received_at
status = ACCEPTED
```

The unique identity is `(source,event_id)`:

- Same event ID and same canonical payload hash: return the original `operation_id` and the exact 202 `ACCEPTED` response without creating another logical mutation.
- Same event ID and a different payload hash: record an audit conflict and return a permanent conflict response. Never project it.
- Deduplication is global to the durable Middleware inbox, not process memory or Redis.

Middleware must validate that the authenticated source, envelope `source`, path, event type, and tenant agree. It propagates `correlation_id`, `causation_id`, and the received W3C trace context to its operation, audit, retry, DLQ, reconciliation, and approved Odoo API calls. It must not put message bodies, email addresses, bearer tokens, or client certificates in trace attributes.

## Odoo projection rules

Middleware is the only component allowed to call the approved Odoo API. Each inbox event produces at most one logical Odoo mutation. Projection writes must be idempotent by the Middleware operation/event identity, use normal Odoo ORM/API validation, and preserve the Klyrow event ID and correlation ID for audit.

Odoo must never be treated as the source of truth for Klyrow delivery, campaign dispatch, domain verification, or metering. Projection failure does not roll back or block Klyrow messaging. Raw messages, recipients, opens, clicks, logs, traces, and metric samples are excluded.

## Reconciliation

Middleware must provide scheduled and operator-triggered reconciliation that compares its durable inbox/operation state with the Odoo projection. The reconciliation record must include range/checkpoint, event count, missing projections, divergent versions, duplicates, repair operation IDs, start/completion times, and outcome. Repairs reuse the original event identity and are audited; they must not blindly create a second Odoo object.

Klyrow exposes durable outbox state and bounded metrics so operators can compare `DELIVERED` events with Middleware inbox counts. Klyrow does not query or repair Odoo directly.

## Outage acceptance requirements

Before enabling the publisher in a production-like environment, prove all of the following:

1. Stop or make Middleware unreachable; Klyrow message admission and provider delivery continue.
2. Produce Klyrow business mutations; all corresponding outbox events remain `PENDING`/`RETRYING` with unchanged hashes and event IDs.
3. Exercise timeouts, 429 with Retry-After, 500/502/503/504, malformed 202, redirects, and permanent 4xx classifications.
4. Restart the Klyrow publisher during an active lease; fencing prevents the stale worker from completing the replacement lease.
5. Restore Middleware; backlog drains to durable 202 acceptances without duplicate inbox operations.
6. Stop Odoo while Middleware remains available; Middleware durably accepts events, Klyrow drains normally, and Middleware's Odoo backlog grows without loss.
7. Restore Odoo; backlog drains with one logical projection per event and reconciliation reports no unexplained drift.
8. Verify the Klyrow worker `/metrics` and private health endpoint without granting either a business-database write path or external public exposure.

Middleware/Odoo exact-tuple runtime acceptance evidence is still required. The paired Middleware source tests are useful implementation evidence, but this Klyrow repository does not fabricate or claim staging/production certification from them.
