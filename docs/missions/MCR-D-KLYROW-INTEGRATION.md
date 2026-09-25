# MCR-D Klyrow campaign execution contract

## Mission and implementation plan

Build dependency-safe contracts from Klyrow main `e4cff8f5b4d84e207e61981f0f24e72c1bfc986f`.
Task authority: PAS-36; MCR-D is assigned to this session by the user.
Notion charter: https://app.notion.com/p/3e57518c3e0681f9a5ead29d6790d0b5
Frozen upstream: Middleware MCR-A `b3f44dd4b8ad8976f10394051d2f13cc17443155`.
The PAS-36 comment links this charter; no competing Builder claim was present in its comments at preflight.

This is a contract-only slice. It adds no registered routes, database migrations,
worker, provider adapter, deployment or production permission. OpenAPI paths are
proposed internal interfaces and must not be advertised as available. The pure
Python reference checks are executable acceptance vectors, not runtime security.

1. Add failing sandbox tests for identity, binding, event mapping, suppression,
   auth and failure/replay decisions.
2. Define strict schemas, offline fixtures and proposed OpenAPI with pinned
   upstream provenance; implement pure reference validation.
3. Run focused tests, offline validator and diff checks; record exact results.
4. Commit/push only if green and terminal Git permits it; verify remote SHA.

## Authority and reuse

| Fact / action | Authority / existing foundation |
| --- | --- |
| Cross-channel plan, policy, frequency caps, cooldown, exposure reservation | Middleware MCR-A/MCR-C; never recomputed by Klyrow |
| Campaign/version/template/audience/sender | `apps/gateway/app/campaign_dispatcher.py`; immutable snapshots |
| Journey node/version/run/waits | `apps/gateway/app/journey_storage.py` (PAS-71); PAS-36 owns semantics |
| Command transport/readback | `apps/gateway/app/middleware_email.py`; existing binding and provider admission |
| Suppression at send time | Existing Klyrow preferences/provider controls plus Middleware authoritative suppression gate |
| Raw delivery inbox / normalized lifecycle projection | Klyrow raw events; Middleware `klyrow_delivery_event_inbox` / MCR-C projection |
| CRM, conversion and human-sales ownership | Odoo; never inferred from an email open |

Klyrow owns email/SMS campaign definitions in MCR-A, but this execution contract
supports email only. SMS transport remains Telnexa. Other channels fail closed.
`EmailCommand.stream` currently accepts only transactional/operational messages.
Marketing MCR touches MUST NOT be coerced to either stream or sent using that
endpoint until its reviewed marketing adapter extension exists.

## Identity and immutable binding

Every touch includes tenant, canonical `klyrow:<raw_id>`, immutable positive
campaign version, canonical lead ID, email channel and 1-based touch index.
The upstream `mcr1:` key is SHA-256 of exactly these six fields as sorted compact
JSON (Python JSON default ASCII escaping, UTF-8). Retry and replay reuse it;
a changed version/touch is a new policy-approved reservation, not a retry.

A bound plan pins command, decision, policy, sender, template/version,
audience snapshot, optional journey/version/node/run, schedule window and
upstream plan hash. Request hash includes all submitted fields. Correlation ID
is required in the transport header; it cannot substitute for tenant identity.
Tenant is derived from verified identity, then matched to header, body and every
stored resource. Foreign objects return 404 after authentication, with no
existence disclosure. Same key/same hash returns the original result; changed
hash is 409 and never overwrites the original binding.

Plan preview cannot reserve, enqueue, schedule, mutate suppressions or call a
provider. The actual execution dependency must re-read the approved immutable
version, sender/brand ownership, consent, global/channel/campaign suppressions,
policy version, schedule, kill switches and tenant limits immediately before
admission. Missing/stale/conflicting evidence blocks admission. Middleware owns
atomic reservation plus command/outbox; Klyrow must bind that existing command
atomically to one message, rather than create a second exposure ledger.

## Events and suppression handoff

`event-mapping.v1.json` is pinned to MCR-A's Klyrow mapping. Accepted, submitted
and sent do not mean delivered. Failed/rejected/cancelled/unknown-outcome are
status reconciliation facts, not invented engagement events. Unknown bounce
classification fails closed as hard bounce. Missing or unrecognized event types
are quarantined. Replies/conversions require their respective authoritative
source; the current Klyrow raw inbox does not accept invented reply events.

Correlate authenticated raw events through the stored command/message binding,
never caller-supplied campaign metadata alone. Match tenant, message and provider
message IDs; retain the full touch identity, correlation and causation. Verify
raw-body signature, timestamp window and unique headers at the existing inbox
before projection. Dedupe by authenticated source/event ID and original inbox
row; same hash is a no-op, different hash is quarantine/409. Received timestamp
is excluded from the normalized digest. MCR-A's prose does not explicitly exclude
the digest field itself: consumers must follow the reviewed MCR-C implementation
before activating a normalized publisher; this slice does not invent a digest.

Hard bounce immediately blocks the affected address locally and hands off channel
health `hard_bounce`; it is NOT an invented MCR suppression reason (the upstream
SuppressionReason enum has no hard_bounce). Complaint/unsubscribe add channel
suppression through Middleware with delivery-event evidence. Global suppression
always wins. No event or replay removes suppression. Delayed delivered/open
cannot clear a bounce, complaint or unsubscribe. Suppression persistence and
outbox handoff must commit atomically; if durability fails, dispatch remains
blocked. A handoff receipt is not proof that Middleware applied the projection;
readback must show acknowledgment or pending reconciliation.

## Failure and replay contract

Known pre-admission transient failures retry with the same key/hash, at most five
attempts (including first), exponential 30/60/120/240 second delays capped at
900 seconds. Retry-After is a lower bound; values above the cap go to operator
review. Timeout after possible acceptance, indeterminate state, missing or
mismatched readback require reconciliation, never blind resubmission. Permanent
validation/auth failures and exhausted retry budgets enter durable dead-letter
state with a safe reason code and original binding. No payload/address/secrets
in error text or telemetry.

Replay is an audited Middleware command, not a direct Klyrow endpoint. It requires
`platform.command.replay`, platform-operator role and fresh verified MFA, an
operator reason, original request hash/key, and revalidation of current policy
and suppression. Delivered or suppressed effects cannot be replayed as sends.
Unknown outcomes reconcile first. Replaying an event reprojects the stored inbox
fact without sending. Readback exposes state independently from delivery and
suppression/engagement outcomes; terminal transport state never regresses.

## Authentication and wiring gates

Public path stays Browser -> Caddy -> Kong -> Middleware -> authorized Klyrow
adapter. No browser/provider shortcut. Middleware public planning uses
`campaign.engine.plan`; read uses `campaign.engine.read` / `leads.journey.read`;
callback publishing uses `campaign.delivery_events.publish`; additive suppression
uses `campaign.suppressions.write`. These are upstream contracts, not proof of
provisioned grants. Keycloak owns signed issuer/audience/AZP/expiry checks and
service-vs-human mapping; no body or forwarded header can grant identity.

Proposed internal preview/read operations require service identity and existing
`klyrow.read`, with an explicit allowlisted Middleware subject and configured
Klyrow audience/AZP. Reject absent configuration, unsigned/expired/wrong issuer,
audience, AZP, human caller, missing scope and cross-tenant requests. Do not reuse
a `middleware-api` audience token directly at Klyrow. MCR-H/J must freeze and
provision the service audience/client binding before runtime registration.
The reference principal represents already-verified claims only; no JWT verifier
or production auth behavior is implemented by these contract tests.

## API/UI wiring expectations

- Proposed `POST /v1/campaign-executions/plan`: validate immutable bindings and
  report a sandbox preview. `GET /v1/campaign-executions/{command_id}`: read the
  original command binding, never trigger a send. No execute/replay route here.
- Existing public MCR planning/readback stays Middleware-owned. UI consumes that
  BFF; server resolves opaque address and object references. No raw addresses,
  message bodies, credential values or provider handles in UI links/telemetry.
- Show accepted/queued/delivered/indeterminate separately, exact campaign version,
  touch, schedule window, safe block reason and suppression handoff status.
- Loading, empty, forbidden, unavailable, stale-plan conflict and reconciliation
  pending must be distinct states. Empty/error must not render as eligible.
- Disable execute/replay affordances until the corresponding capability and
  separately verified implementation exist. Preview never implies send approval.
- Metrics reuse existing dispatcher/provider instruments; low-cardinality status
  and reason labels only. IDs belong in access-controlled traces/audit, not labels.

## Activation dependencies and merge order

MCR-A pin -> MCR-C durable policy/reservation/readback/event implementation ->
MCR-H/J verified service/callback scope binding -> reviewed Klyrow marketing
adapter/binding persistence -> API/BFF/UI integration -> sandbox restart,
concurrency, signature/auth and suppression-race certification. PAS-71 storage
is merged; PAS-36 runtime semantics and PAS-218 UI remain separately governed.
This contract's tests do not certify PostgreSQL concurrency, runtime JWTs,
provider delivery or staging. All provider/live/money/deploy/merge effects remain
OFF. Reconcile this pinned contract with the accepted MCR-C head before activation.
Rollback of this slice removes only contract artifacts; no runtime state changes.
