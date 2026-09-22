# PRO-S2 Communications — Authority and Contract Freeze

Mission: PAS-211 / PRO-S2.A

Status: Gate A source contract freeze.

## Source identity

- Protected main baseline: `ca54d5567d719d7aa09098dca1d5e85c3bd76903`.
- Webmail source authority: PR #165, `mission/pas195-webmail-certification-20260921@8295f7cc0f6bd1bde735da9620fe6cdd3498fe4a`.
- Webmail/Postal observability authority: PR #166, `mission/pas198-webmail-postal-observability-20260921@8c9af4fe54df1a43cf1b8eb867d8a1aeb2697da5`.
- This freeze is intentionally stacked on PR #166 so it inherits PAS-195 and PAS-198 rather than rebuilding either surface.
- PAS-197 Administration & Operations remains a separate active mission. Its admin route/page files are not Communications implementation authority and must be reconciled before editing overlapping admin UI files.

Architecture remains:

**Client / Browser → Caddy → Kong → Middleware → authorized adapter → service/provider**

No Communications work may create a direct browser-to-Postal path, a second provider adapter, a second queue, a second audit store, a second metrics stack, or a new identity authority.

---

## Gate 01 — Current-state inventory

### Domain and sender product authority

Existing same-origin browser BFF:

- `GET /app/api/domains`
- `POST /app/api/domains`
- `POST /app/api/domains/{item_id}/verify`
- `GET /app/api/senders`
- `POST /app/api/senders`

The browser façade is `apps/gateway/app/browser_email_setup.py` and deliberately reuses `apps/gateway/app/messaging.py`.

Existing durable models:

- `messaging.DomainClaim` — browser/product domain lifecycle and DNS ownership state.
- `messaging.SenderIdentity` — browser/product sender identity.
- `messaging.DkimKeyVersion` — DKIM version history/reference.
- `main.Domain` and `main.AllowedSender` — existing core compatibility/send-enforcement projections used by the governed mail path.
- provider-layer domain/sender models — transport/provider projections only; they are not a second product authority.

Current portal state:

- `/app/email/domains` implemented.
- `/app/email/senders` implemented.
- `/app/email/domains/:id` partial because detail/evidence is missing.

### Suppression and consent authority

Existing same-origin browser BFF:

- `GET /app/api/suppressions?limit&offset`
- `POST /app/api/suppressions`
- `DELETE /app/api/suppressions/{suppression_id}`

Existing durable authorities:

- `main.Suppression` — tenant/global recipient suppression used by send-time enforcement.
- `preferences.ScopedSuppression` — existing scoped suppression model.
- existing Profile / Consent / Preference models — consent and subscription-topic authority.

The Communications suite must extend these records. It must not create a second suppression or consent table.

### Webmail/shared inbox authority

PAS-195 freezes the Webmail authority in:

- `apps/gateway/app/webmail.py`
- `apps/gateway/app/webmail_models.py`
- `apps/web/src/Webmail.vue`
- `migrations/2026092105_webmail_rls.sql`

Durable models:

- `WebmailMailbox`
- `WebmailAccess`
- `WebmailMessage`
- `WebmailAttachment`

Existing access roles are `OWNER`, `SENDER`, and `READER`. Tenant managers can access tenant mailboxes; non-manager users require an explicit mailbox grant.

Existing Webmail BFF contract:

| Method | Path | Existing purpose |
| --- | --- | --- |
| GET | `/app/api/mailboxes` | access-scoped mailbox list/counts |
| POST | `/app/api/mailboxes/sync` | materialize verified senders |
| POST | `/app/api/mailboxes/inbound/activate` | reconcile exact inbound routes |
| GET | `/app/api/mailboxes/{mailbox_id}/messages` | folder/search list |
| GET | `/app/api/mailboxes/{mailbox_id}/messages/{message_id}` | message detail |
| GET | `/app/api/mailboxes/{mailbox_id}/messages/{message_id}/attachments/{attachment_id}` | attachment download |
| POST | `/app/api/mailboxes/{mailbox_id}/drafts` | create draft |
| PUT | `/app/api/mailboxes/{mailbox_id}/drafts/{message_id}` | update draft |
| POST | `/app/api/mailboxes/{mailbox_id}/send` | governed transactional send |
| PATCH | `/app/api/mailboxes/{mailbox_id}/messages/{message_id}` | read/star/folder state |
| DELETE | `/app/api/mailboxes/{mailbox_id}/messages/{message_id}` | trash/permanent delete |
| GET | `/app/api/mailboxes/{mailbox_id}/access` | list grants |
| POST | `/app/api/mailboxes/{mailbox_id}/access` | grant OWNER/SENDER/READER |
| DELETE | `/app/api/mailboxes/{mailbox_id}/access/{user_id}` | revoke grant |

**Freeze:** “shared/team inbox” is a product presentation of `WebmailMailbox + WebmailAccess`. No second shared-inbox mailbox, membership, message, credential, or password model is permitted.

### Delivery evidence authority

Existing durable state includes:

- `main.EmailOutbox` — accepted outbound work/idempotent queue identity.
- `main.Message` / `main.Event` — core message/event projection.
- `main.PostalEvent` — authenticated Postal callback evidence.
- `provider.ProviderMessage` / `provider.ProviderEvent` — provider-layer durable message/event evidence.
- Webmail outbound delivery status is projected back to `WebmailMessage.delivery_status`.

Provider callbacks are evidence only. They never become tenant, mailbox, sender, or authorization truth.

### Deliverability authority

Existing durable/read logic:

- `saas.DeliverabilitySnapshot` with SPF, DKIM, DMARC, MX, PTR, TLS, details and checked timestamp.
- `POST /v1/deliverability/domains/{domain_id}/check`.
- `GET /v1/deliverability`.
- current portal `/app/deliverability` and `/app/deliverability/domains/:id` are partial and currently read only domain-claim state.

PAS-198 owns Webmail/Postal operational SLOs, alerts, Grafana, incidents and trace readback. PRO-S2 must not duplicate those observability assets.

---

## Gate 02 — Authority map

| Fact/component | Authority | PRO-S2 action |
| --- | --- | --- |
| Human authentication, MFA, recovery | Keycloak / existing OIDC boundary | REUSE |
| Tenant, membership, roles | Klyrow Organization/Identity authority | REUSE |
| Browser session/CSRF | existing Klyrow BFF | REUSE |
| Domain lifecycle | `messaging.DomainClaim` | EXTEND read detail only |
| Core verified-domain compatibility | `main.Domain` | REUSE; no new product table |
| DKIM versions | `messaging.DkimKeyVersion` | REUSE/read |
| Sender product identity | `messaging.SenderIdentity` | EXTEND read detail only |
| Send-enforcement sender allowlist | `main.AllowedSender` | REUSE |
| Suppression | `main.Suppression` / existing scoped suppression | REUSE/EXTEND filters only |
| Consent/preferences | existing Profile/Consent/Preference | REUSE |
| Mailbox | `WebmailMailbox` | REUSE |
| Shared/team mailbox membership | `WebmailAccess` | REUSE |
| Mailbox message | `WebmailMessage` | REUSE |
| Outbound accepted work | `EmailOutbox` | REUSE |
| Provider callback evidence | `PostalEvent` / `ProviderEvent` | REUSE/read only |
| Deliverability snapshot | `DeliverabilitySnapshot` | REUSE |
| Cross-system provider mutation | Middleware authorized adapter | REUSE; sole mutation path |
| Postal transport | Postal | provider only |
| SLO/alerts/traces | PAS-198 observability assets | REUSE |
| Admin Communications projection | PAS-197 | REUSE; no parallel admin console |

Any ownership collision discovered during Gate B stops implementation until the owning mission is reconciled.

---

## Gate 03 — Domain, state and failure model

### Domain

Canonical provider-domain lifecycle vocabulary already exists:

`PENDING → DNS_REQUIRED → VERIFYING → VERIFIED → SENDING_ENABLED`

Exceptional terminal/blocked states:

`SUSPENDED`, `REMOVED`.

Rules:

1. A domain is tenant-owned product state in Klyrow.
2. DNS verification must never be fabricated from stale/provider-only state.
3. “Verified” does not imply unrestricted production delivery.
4. Provider readiness and product verification are separate facts and must be shown separately.
5. Read APIs may surface provider/readiness projections but may not mutate Postal directly.

### Sender

Existing sender lifecycle uses:

`PENDING → ACTIVE`, with `SUSPENDED` and `REMOVED`.

Rules:

1. Sender belongs to one tenant and one domain claim.
2. Sender address must remain unique within the tenant.
3. Browser management reuses the existing sender creation path.
4. Product sender state and provider transport projection must not be conflated.
5. Sending still requires the governed mail policy path, verified domain, sender authorization, tenant controls and suppression checks.

### Shared/team mailbox

A shared/team inbox is one `WebmailMailbox` plus zero or more `WebmailAccess` grants.

Grant roles:

- `OWNER`: mailbox management/send/read.
- `SENDER`: send/read.
- `READER`: read only.

Rules:

- tenant isolation is mandatory;
- non-manager access without a grant is hidden as not found;
- no mailbox owns an independent password;
- access changes remain CSRF-protected;
- grant/revoke must remain idempotent or conflict-safe;
- no direct Postal ACL is product authority.

### Suppression

Hard/global reasons continue to fail closed in the existing send guard. Marketing delivery additionally depends on existing stored consent/preferences.

Rules:

- transactional mail never bypasses hard/global suppressions;
- marketing mail requires valid consent/preference state;
- duplicate add with the same reason is safe;
- reason conflict remains a conflict, not an overwrite;
- removal is tenant-scoped and must be audited in Gate B;
- browser/API responses never expose unrelated recipient data.

### Delivery evidence

Canonical product outcome vocabulary must normalize provider outcomes into:

`QUEUED | PROCESSING | SUBMITTED | SENT | DELIVERED | DEFERRED | INDETERMINATE | BOUNCED_SOFT | BOUNCED_HARD | COMPLAINED | SUPPRESSED | FAILED | DEAD_LETTER`.

Rules:

- provider callbacks are append/readback evidence, not write authority for tenant config;
- duplicate provider events are replay-safe;
- ambiguous provider outcomes remain `INDETERMINATE` until reconciled;
- retry is never inferred from a GET/read operation;
- evidence responses are redacted and tenant scoped;
- correlation/request IDs link browser request → durable outbox → provider evidence.

### Failure semantics

Stable browser error codes:

- `401`: no valid browser session.
- `403`: authenticated but missing role/capability/CSRF authorization.
- `404`: tenant-scoped resource absent or intentionally hidden cross-tenant.
- `409`: idempotency/state/reason/ownership conflict.
- `422`: invalid domain/sender/state/DNS evidence.
- `429`: applicable rate limit.
- `502`: authenticated downstream/readback response is incomplete or invalid.
- `503`: required Middleware/provider/DNS dependency unavailable for an explicitly requested operation.

Read-only pages degrade honestly when provider/readiness evidence is unavailable; they must not silently convert unknown into healthy.

---

## Gate 04 — API/event contract freeze

Gate B may implement only the following missing/extension contracts.

### A. Domain detail

`GET /app/api/domains/{domain_id}`

Audience: tenant browser session with `mail.read`.

Response:

```json
{
  "id": "domain-claim-id",
  "domain": "example.com",
  "state": "VERIFIED",
  "verified_at": "ISO-8601|null",
  "suspended_at": "ISO-8601|null",
  "dkim": {
    "selector": "kly...",
    "version": 1,
    "history": [
      {"selector": "kly...", "version": 1, "active": true, "created_at": "ISO-8601", "retired_at": null}
    ]
  },
  "dns": {
    "return_path": "bounce.example.com",
    "tracking_domain": "track.example.com"
  },
  "deliverability": {
    "source": "durable_snapshot|none",
    "checked_at": "ISO-8601|null",
    "spf": true,
    "dkim": true,
    "dmarc": true,
    "mx": true,
    "ptr": false,
    "tls": false,
    "alerts": []
  },
  "provider_readiness": {
    "sending_enabled": false,
    "inbound_enabled": false,
    "status": "unknown|VERIFIED|SENDING_ENABLED|SUSPENDED"
  }
}
```

No secret token, private DKIM reference, provider credential or raw callback payload may be returned.

### B. Sender detail

`GET /app/api/senders/{sender_id}`

Audience: tenant browser session with `mail.read`.

Response must include product sender identity, its domain-claim summary and safe readiness only:

- `id`
- `address`
- `display_name`
- `reply_to`
- `stream`
- `status`
- `verified`
- `domain: {id, domain, state}`
- `mailbox: {id|null, sending_enabled, receiving_enabled, shared_grant_count}`

No provider credential material.

### C. Deliverability list/detail

`GET /app/api/deliverability?limit&offset&state`

Tenant-scoped list of domain claims joined to the latest durable `DeliverabilitySnapshot`. GET performs no live DNS/provider mutation.

Response:

```json
{
  "items": [],
  "limit": 50,
  "offset": 0,
  "has_more": false
}
```

Each item carries domain id/name/state, checked_at, SPF/DKIM/DMARC/MX/PTR/TLS, alert count and `stale`.

`GET /app/api/deliverability/domains/{domain_id}`

Returns the same safe detail plus bounded recent snapshot history.

`POST /app/api/deliverability/domains/{domain_id}/check`

Audience: OWNER/ADMIN with CSRF and `Idempotency-Key`.

This operation may run the existing Klyrow DNS/deliverability checker and persist a snapshot. It must not directly mutate Postal. Any provider-side remediation remains a Middleware command owned outside this endpoint.

### D. Delivery evidence

`GET /app/api/messages/{message_id}`

Audience: tenant browser session with `mail.read`.

Extends the existing message-list product surface with:

- core message fields;
- normalized current outcome;
- safe outbox state/attempt count/timestamps;
- bounded normalized event timeline;
- safe provider evidence summary;
- correlation/request IDs where present.

Raw provider payload, recipient secrets, credentials, internal tokens and cross-tenant records are forbidden.

`GET /app/api/messages/{message_id}/events?limit&cursor`

Cursor-paginated normalized evidence timeline. No GET side effects.

### E. Existing suppression contract extensions

Existing paths remain authoritative. Gate B may add optional filters only:

`GET /app/api/suppressions?limit&offset&q&reason`

Do not create `/communications/suppressions` or another resource family.

Mutation requirements:

- CSRF;
- `Idempotency-Key` on add;
- tenant isolation;
- audit event on add/remove;
- stable 404/409 behavior.

### F. Shared/team inbox

No new mailbox resource family.

Gate B reuses the existing `/app/api/mailboxes/{mailbox_id}/access` endpoints and may extend mailbox-list/detail responses with presentation-safe metadata:

- `is_shared`
- `grant_count`
- `my_access_role`
- `unread_count`
- `sending_enabled`
- `receiving_enabled`

No second mailbox grant API is authorized.

### Event contract

PRO-S2 consumes existing durable event/outbox evidence. It does not introduce a new event bus.

Required correlation fields where available:

- `tenant_id`
- `message_id`
- `operation_id`
- `correlation_id`
- normalized `kind/status`
- `occurred_at`

Provider/raw payloads are stored only in their existing durable authority and are never emitted through the browser BFF.

### OpenAPI/Postman rule

Gate B must regenerate the canonical browser OpenAPI/Postman inventories after route changes and verify that:

- each new browser route is same-origin;
- audience/auth is explicit;
- mutation CSRF/idempotency requirements are represented;
- list endpoints are bounded/paginated;
- error examples contain stable safe codes;
- no raw Postal/provider admin API is exposed as the commercial/browser surface.

---

## Gate 05 — UX/design contract freeze

### Information architecture

Keep the existing portal grouping. PRO-S2 may complete, not duplicate:

- `/app/email/messages`
- `/app/email/messages/:id`
- `/app/email/domains`
- `/app/email/domains/:id`
- `/app/email/senders`
- `/app/email/inbound`
- `/app/email/suppressions`
- `/app/deliverability`
- `/app/deliverability/domains/:id`
- existing `/app/mail` Webmail application

No second “communications center” route is authorized unless this freeze is revised.

### Shared/team inbox UX

The existing Webmail UI is extended to make mailbox sharing explicit:

- mailbox label/address;
- my access role;
- shared badge/grant count;
- read/send capability indication;
- manager-only access management;
- clear send-ready and receive-ready states.

No hidden/manual-only grant workflow may be required for normal tenant managers once Gate B is complete.

### Domain/deliverability UX

Domain detail must clearly separate:

1. ownership state;
2. DNS evidence;
3. sending readiness;
4. inbound readiness;
5. latest deliverability evidence;
6. stale/unknown evidence;
7. operator/admin-only remediation boundaries.

A yellow/unknown state is preferred to a false healthy state when evidence is stale or missing.

### Message evidence UX

Message detail shows a bounded human-readable timeline:

Accepted → queued → submitted → provider evidence → delivered/bounced/complained/indeterminate.

It must distinguish product acceptance from final delivery.

### Required page states

Every PRO-S2 page/critical component must define and test:

- loading;
- empty;
- ready;
- degraded/stale;
- forbidden;
- error;
- mobile/responsive.

Mutations additionally need:

- idle;
- submitting;
- success;
- conflict/validation failure;
- retry-safe failure.

### Accessibility/privacy

- keyboard reachable actions;
- visible focus;
- associated labels;
- non-color-only status;
- status changes announced where appropriate;
- responsive tables/cards;
- no secret/provider credential/raw payload leakage;
- no unnecessary full recipient/body exposure on aggregate pages.

---

## Gate A handoff

PAS-211 authorizes Gate B to:

1. add browser **detail/evidence** routes above;
2. extend existing suppressions with bounded filters/audit;
3. enhance existing Webmail mailbox/access presentation as the shared/team inbox;
4. complete deliverability/detail UI from existing durable authorities;
5. add focused authorization, tenant isolation, idempotency, degraded-provider, mobile/accessibility and browser E2E coverage;
6. regenerate browser contracts.

PAS-211 does **not** authorize:

- rebuilding Webmail lifecycle from PAS-195;
- rebuilding PAS-198 observability;
- creating a new domain/sender/suppression/mailbox authority;
- direct Postal/provider writes from browser APIs;
- production delivery activation;
- bypassing Middleware for cross-system business effects;
- editing active PAS-197 admin ownership without reconciliation.

Production/external delivery remains governed separately. Gate B source completion must not be described as runtime or production certification without exact immutable staging evidence.
