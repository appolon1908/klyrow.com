# Middleware email command bridge

PR #89 patches the Klyrow-owned files imported from Middleware commit
`8336a9de25fe7cf6c6af720ac1f79e98ed345f02`. SOURCE.json preserves that upstream
identity; the owned files now contain the reviewed local corrections.

## Implemented contract

`POST /v1/email/messages` accepts one rendered transactional message or the
fixed-recipient operational alert. It requires `Idempotency-Key`,
`X-Correlation-ID`, and the existing authenticated service context. The canonical
tenant resolver must authorize `klyrow.send` for submission and `klyrow.read` for
read-back. Token scopes alone do not grant these resolver permissions.

The input `message_id` is the Middleware command ID. Klyrow generates a separate
message ID. The response and `GET /v1/email/messages/{command_id}` return both
`command_id` and `message_id`, tenant, correlation, sender, recipient, current
status, and SHA-256 of the full request document serialized as sorted compact
JSON. Adapters compare every binding before recording acceptance. Timeout
recovery reads the command ID and returns the actual Klyrow message ID.

The binding scopes command and idempotency identities to caller and tenant.
Reusing either identity with changed body, correlation, or the other identity
returns 409. A tenant row lock serializes concurrent duplicate submissions.
The binding is committed in the same database transaction as the existing
governed send: native idempotency, message, usage, lifecycle events and (when
enabled) durable outbox. Policy denial rolls back the binding.

When Klyrow is not in safe mode, this route additionally requires Middleware's
active transactional-production attestation. It is bound to the exact command
ID, correlation ID, idempotency-key digest, tenant, sender, recipient-list
digest, category, policy version, change ID, validity window, provider and exact
Middleware release. Campaign mode, an expired/revoked authority, a closed kill
switch, a changed command binding, and malformed or extra fields fail closed.

The recipient digest uses SHA-256 over UTF-8 compact JSON of the sorted,
trimmed, lowercase addresses. Duplicate addresses are retained in the digest.
The current single-recipient Middleware transport remains compatible; batch
transport still needs a coordinated producer contract before activation.
All policy timestamps and the execution clock require a defined timezone
offset and are compared as UTC instants. The validity window includes
`validFrom` and excludes `validUntil`; naive timestamps are rejected.

Middleware is the sole production-policy authority; Klyrow does not store or
mutate a second policy. Klyrow persists the validated attestation with the
outbox intent and checks it again immediately before Postal submission. The
control document is removed from the provider payload and never sent to Postal.
Direct Klyrow send routes cannot provide this private authorization argument and
remain governed by the existing one-message canary gate.

Production also binds the tenant-resolver `identity_id` to the exact dedicated
service configured as `KLYROW_MIDDLEWARE_EMAIL_IDENTITY`. Missing configuration
returns 503 and a different service identity returns 403 even if it holds an
unrelated `klyrow.send` grant.

Batch recipients, template references and scheduled submission are rejected
before transport. They need separate contracts before activation. Alert payloads
require all evidence fields and a string label map; recipient/sender policies
remain fixed. Malformed JSON event objects return 422 without database work.
Malformed Odoo JSON-RPC responses consume the bounded retry budget and release
the worker lease, eventually reaching dead letter.

## Validation

The required `test` CI job runs the gateway suite, applies all migrations twice,
runs concurrent PostgreSQL replay tests, and tests the owned integration files
against the immutable Middleware dependency package and real gateway ASGI routes.
No external delivery is required. The round-trip test interrupts the HTTP response
after the database commit and proves read-back finds one message.

To run the adapter tests locally, use the gateway requirements plus test-only
`temporalio==1.32.0`, `asyncpg==0.30.0`, `pydantic-settings==2.15.0`, and
`pytest-asyncio==1.4.0`. Check out the exact source SHA in a separate clean
Middleware checkout, then run:

```sh
python -m pytest -q tests/test_middleware_email_contract.py tests/test_openapi_authority.py
python scripts/verify_middleware_contract.py /path/to/Middleware-checkout
```

The script overlays only this repository's owned files in a temporary directory.
The original dependency checkout stays unchanged. PostgreSQL locking tests run
when `KLYROW_CONTRACT_POSTGRES_URL` points to an isolated test database with schema
creation privileges; the fixture creates and drops a uniquely named schema.

## Migration and rollback

`2026090601_middleware_email_command_bindings.sql` is additive and grants the
existing runtime role access to the new table. Run the normal ledger-backed
migrator before starting the new gateway. Older gateway images ignore this table;
rollback restores the prior image and disables the adapter capability. Preserve
the binding table during rollback so accepted commands retain their identities.
Do not retry unknown outcomes through the old incompatible adapter.

## Remaining activation gates

This implementation does not certify production readiness for issue #85. Private
TLS/mTLS routing, service audience and resolver grants, immutable deployment
evidence, a reviewed synthetic canary and rollback evidence are still required.
The existing one-message gateway production canary continues to govern every
non-Middleware route. The canonical Middleware route can move beyond that
consumed allowance only with the validated command-bound production attestation.
Connector activation defaults remain disabled. This source change itself does
not authorize live email, deployment, Keycloak configuration or external Odoo
mutation.
