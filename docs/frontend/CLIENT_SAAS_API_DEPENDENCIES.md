# Client SaaS portal — API dependency matrix (Phase 10A)

Discovery baseline: `origin/main` at `fba9101416e0102f7d9b9f35c2256b85b8c18cf0`,
branch `phase/10a-client-saas-portal`, worktree `klyrow-client-portal`.

## Browser boundary

The browser may only call same-origin `/app/api/*` and `/auth/*` through
`apps/web/src/api.ts` (`appApi`). Nginx proxies `/v1/*`, but those handlers
require a Bearer API key or OIDC access token that the browser session never
receives, so `/v1/*` is **not** a browser dependency. A page is therefore only
"backed" when a `/app/api/*` or `/auth/*` route exists in
`docs/api/runtime-routes.json`.

Platform-admin authority is not present in the `/auth/session` body (its
`role` is the tenant membership role). The portal proves it server-side by
reading `GET /app/api/admin/dashboard` (`403 platform_admin_required`
otherwise). Hidden navigation is never treated as authorization.

## Existing browser routes consumed

| Route | Used by |
| --- | --- |
| `GET /auth/session` | session guard |
| `POST /auth/logout`, `POST /auth/logout-all` | user menu, security page |
| `GET /auth/sessions`, `DELETE /auth/sessions/{session_id}` | security page |
| `GET /app/api/context` | organization page, organization switcher |
| `POST /app/api/organizations/{tenant_id}/switch` | organization switcher |
| `GET /app/api/dashboard` | overview, usage, analytics overview, plan |
| `GET /app/api/onboarding` | overview onboarding progress |
| `GET /app/api/messages?limit&offset` | email messages list, message summary, developer logs |
| `GET/POST /app/api/domains`, `POST /app/api/domains/{item_id}/verify` | domains, domain detail, deliverability |
| `GET/POST /app/api/senders` | senders |
| `GET /app/api/mailboxes`, `POST /app/api/mailboxes/inbound/activate` | inbound |
| `GET /app/api/provisioning/postal` | overview provisioning status |
| `GET /app/api/team`, `POST /app/api/team/invitations` | team page |
| `GET /app/api/admin/dashboard` | platform-admin authority probe, admin system/queues/tenants/deliverability counts |
| `GET /app/api/admin/provisioning/postal` | admin deliverability provisioning failures |

## Route classification

Status values: IMPLEMENTED (existing API fully usable), PARTIAL (existing API
covers part of the page; the rest shows an unavailable state), MISSING (no
browser API; the page renders an honest unavailable state with the required
contract), BLOCKED (would need a backend capability that does not exist in any
audience; same UI as MISSING, recorded for the backend owner).

| Route | Status | Browser API | Missing contract |
| --- | --- | --- | --- |
| `/app/overview` | PARTIAL | dashboard, onboarding, provisioning/postal, domains, senders | deliverability summary, incidents, plan summary have no browser API |
| `/app/email/messages` | IMPLEMENTED | `GET /app/api/messages` (limit/offset) | cursor pagination and server-side status filter (client-side filter over the loaded page) |
| `/app/email/messages/:id` | IMPLEMENTED | `GET /app/api/messages/{message_id}` and `GET /app/api/messages/{message_id}/events` | — |
| `/app/email/streams` | MISSING | — | `GET/POST /app/api/streams` |
| `/app/email/domains` | IMPLEMENTED | domains list/create/verify | — |
| `/app/email/domains/:id` | IMPLEMENTED | `GET /app/api/domains/{item_id}` plus deliverability check action | — |
| `/app/email/senders` | IMPLEMENTED | senders list/create | suspension/approval actions |
| `/app/email/inbound` | IMPLEMENTED | mailboxes list, inbound activate | inbound route listing |
| `/app/email/suppressions` | MISSING | — | `GET/POST/DELETE /app/api/suppressions` |
| `/app/content/templates` | MISSING | — | `GET/POST /app/api/templates` |
| `/app/content/templates/:id` | MISSING | — | `GET /app/api/templates/{id}` + versions |
| `/app/content/builder/:id` | MISSING | — | template version publish/rollback/render |
| `/app/content/media` | BLOCKED | — | no media API exists in any audience |
| `/app/content/brand` | BLOCKED | — | no brand API exists in any audience |
| `/app/audience/profiles` | MISSING | — | `GET /app/api/profiles` |
| `/app/audience/profiles/:id` | MISSING | — | `GET /app/api/profiles/{id}` + timeline |
| `/app/audience/imports` | MISSING | — | `GET/POST /app/api/imports` |
| `/app/audience/segments` | MISSING | — | `GET/POST /app/api/segments` |
| `/app/audience/segments/:id` | MISSING | — | `GET /app/api/segments/{id}` + preview |
| `/app/audience/preferences` | MISSING | — | `GET /app/api/preferences` |
| `/app/campaigns` | MISSING | — | `GET /app/api/campaigns` |
| `/app/campaigns/new` | MISSING | — | `POST /app/api/campaigns` |
| `/app/campaigns/:id` | MISSING | — | campaign detail and lifecycle actions |
| `/app/journeys` | MISSING | — | `GET /app/api/journeys` |
| `/app/journeys/new` | MISSING | — | `POST /app/api/journeys` |
| `/app/journeys/:id/builder` | MISSING | — | journey graph read/write |
| `/app/journeys/:id/runs` | MISSING | — | journey run history |
| `/app/analytics/overview` | PARTIAL | dashboard metrics | historical aggregation |
| `/app/analytics/campaigns` | MISSING | — | campaign analytics |
| `/app/analytics/journeys` | MISSING | — | journey analytics |
| `/app/analytics/segments` | MISSING | — | segment analytics |
| `/app/analytics/links` | MISSING | — | link analytics |
| `/app/deliverability` | IMPLEMENTED | `GET /app/api/deliverability` | — |
| `/app/deliverability/domains/:id` | IMPLEMENTED | `GET /app/api/deliverability/domains/{item_id}` and `POST /app/api/deliverability/domains/{item_id}/check` | — |
| `/app/deliverability/ip-pools` | MISSING | — | IP pool/warmup state |
| `/app/deliverability/alerts` | MISSING | — | alert feed |
| `/app/developer/api-keys` | MISSING | — | `GET/POST/DELETE /app/api/api-keys` (+rotate) |
| `/app/developer/service-accounts` | MISSING | — | service-account CRUD |
| `/app/developer/smtp` | MISSING | — | SMTP credential CRUD |
| `/app/developer/webhooks` | MISSING | — | webhook subscription CRUD and delivery history |
| `/app/developer/logs` | PARTIAL | `GET /app/api/messages` | operation log API |
| `/app/developer/openapi` | MISSING | — | same-origin OpenAPI document endpoint |
| `/app/billing/plan` | PARTIAL | dashboard quota | subscription/plan read API |
| `/app/billing/usage` | PARTIAL | dashboard metrics | usage history API |
| `/app/billing/invoices` | MISSING | — | invoice list |
| `/app/billing/invoices/:id` | MISSING | — | invoice detail |
| `/app/billing/payment-methods` | MISSING | — | payment-method listing (live payment actions disabled by design) |
| `/app/settings/organization` | IMPLEMENTED | context | organization edit |
| `/app/settings/team` | IMPLEMENTED | team, invitations | member role change/removal |
| `/app/settings/security` | IMPLEMENTED | sessions, logout-all | MFA enrolment (Keycloak-owned) |
| `/app/settings/sso` | BLOCKED | — | not implemented server-side |
| `/app/settings/scim` | BLOCKED | — | not implemented server-side |
| `/app/settings/retention` | MISSING | — | retention policy browser API |
| `/app/settings/audit` | MISSING | — | audit read browser API |
| `/app/settings/integrations` | MISSING | — | integration browser API |
| `/app/support` | MISSING | — | support ticket API |
| `/app/support/tickets/:id` | MISSING | — | support ticket detail |
| `/admin/tenants` | PARTIAL | admin dashboard counts | tenant listing |
| `/admin/deliverability` | PARTIAL | admin dashboard, admin provisioning failures | platform DNS/reputation |
| `/admin/abuse` | MISSING | — | abuse review API |
| `/admin/queues` | PARTIAL | admin dashboard outbox counts | queue topology |
| `/admin/reconciliation` | MISSING | — | reconciliation API |
| `/admin/billing` | MISSING | — | platform billing API |
| `/admin/system` | IMPLEMENTED | admin dashboard | certification evidence |
| `/admin/audit` | MISSING | — | platform audit API |

Existing root views that stay untouched: `/app` (legacy command center),
`/app/mail` (webmail), `/onboarding`, `/app/provisioning`,
`/admin` (legacy admin overview), `/admin/provisioning`, and all public
authentication routes.

## Files owned by this mission

- `apps/web/src/Portal.vue`, `apps/web/src/portal/**`
- `apps/web/src/routeManifest.ts`, `apps/web/src/main.ts` (Portal root registration)
- `apps/web/src/api.ts` (typed `ApiError` carrying status and request ID; behaviour otherwise unchanged)
- `apps/web/src/__tests__/portal*.test.ts`, `apps/web/src/__tests__/apiErrors.test.ts`
- `apps/web/e2e/portal.spec.ts`, `apps/web/e2e/server.mjs` (cross-platform path fix only)
- `docs/frontend/*`
