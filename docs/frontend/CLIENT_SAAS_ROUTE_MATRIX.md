# Client SaaS portal — route matrix (Phase 10A)

Generated from `apps/web/src/portal/routes.ts` (the single source of truth for
route metadata). Regenerate with the script in `docs/frontend/CLIENT_SAAS_TEST_EVIDENCE.md`
or edit the route table and re-run the route contract tests.

Access column: the session capability (`*` grants all) or membership role the page
requires before it renders. Platform-admin routes additionally require server-proven
authority (`GET /app/api/admin/dashboard` → 200). Server authorization remains
authoritative for every call.

| Route | Group | Title | Audience | Access | Status | Browser APIs | Dependency / missing contract |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `/app/overview` | Overview | Overview | tenant | `session` | PARTIAL | `GET /app/api/dashboard`, `GET /app/api/onboarding`, `GET /app/api/provisioning/postal`, `GET /app/api/domains`, `GET /app/api/senders` | Readiness, usage, onboarding and provisioning come from existing browser APIs; deliverability, incidents and plan detail have no browser API. |
| `/app/email/messages` | Email | Messages | tenant | `mail.read` | IMPLEMENTED | `GET /app/api/messages` | GET /app/api/messages supports limit/offset paging; status filtering is applied to the loaded page in the browser. |
| `/app/email/messages/:id` | Email | Message | tenant | `mail.read` | PARTIAL | `GET /app/api/messages` | Summary fields come from the message list; the event timeline, retry and cancel need GET /app/api/messages/{id}. |
| `/app/email/streams` | Email | Streams | tenant | `mail.read` | MISSING | — | No browser API exists yet. Required contract: GET/POST /app/api/streams. |
| `/app/email/domains` | Email | Domains | tenant | `mail.read` | IMPLEMENTED | `GET /app/api/domains`, `POST /app/api/domains`, `POST /app/api/domains/{item_id}/verify` | Domain claims, creation and DNS ownership verification use existing browser APIs. |
| `/app/email/domains/:id` | Email | Domain | tenant | `mail.read` | PARTIAL | `GET /app/api/domains`, `POST /app/api/domains/{item_id}/verify` | Claim state and identifiers come from the domain list; SPF/DMARC/PTR/TLS evidence and DKIM history need GET /app/api/domains/{id}. |
| `/app/email/senders` | Email | Senders | tenant | `mail.read` | IMPLEMENTED | `GET /app/api/senders`, `POST /app/api/senders`, `GET /app/api/domains` | Sender identities and creation use existing browser APIs. |
| `/app/email/inbound` | Email | Inbound | tenant | `mail.read` | IMPLEMENTED | `GET /app/api/mailboxes`, `POST /app/api/mailboxes/inbound/activate` | Mailbox readiness and inbound activation use the webmail browser APIs. |
| `/app/email/suppressions` | Email | Suppressions | tenant | `mail.read` | MISSING | — | No browser API exists yet. Required contract: GET/POST/DELETE /app/api/suppressions. |
| `/app/content/templates` | Content | Templates | tenant | `campaign.manage` | MISSING | — | No browser API exists yet. Required contract: GET/POST /app/api/templates. |
| `/app/content/templates/:id` | Content | Template | tenant | `campaign.manage` | MISSING | — | No browser API exists yet. Required contract: GET /app/api/templates/{id} with version history. |
| `/app/content/builder/:id` | Content | Builder | tenant | `campaign.manage` | MISSING | — | No browser API exists yet. Required contract: template version create/publish/rollback/render browser APIs. |
| `/app/content/media` | Content | Media | tenant | `campaign.manage` | BLOCKED | — | No browser API exists yet. Required contract: a media library API (none exists in any audience). |
| `/app/content/brand` | Content | Brand | tenant | `campaign.manage` | BLOCKED | — | No browser API exists yet. Required contract: a brand settings API (none exists in any audience). |
| `/app/audience/profiles` | Audience | Profiles | tenant | `contact.manage` | MISSING | — | No browser API exists yet. Required contract: GET /app/api/profiles. |
| `/app/audience/profiles/:id` | Audience | Profile | tenant | `contact.manage` | MISSING | — | No browser API exists yet. Required contract: GET /app/api/profiles/{id} with timeline, consent and preferences. |
| `/app/audience/imports` | Audience | Imports | tenant | `contact.manage` | MISSING | — | No browser API exists yet. Required contract: GET/POST /app/api/imports. |
| `/app/audience/segments` | Audience | Segments | tenant | `contact.manage` | MISSING | — | No browser API exists yet. Required contract: GET/POST /app/api/segments. |
| `/app/audience/segments/:id` | Audience | Segment | tenant | `contact.manage` | MISSING | — | No browser API exists yet. Required contract: GET /app/api/segments/{id} with preview; membership rebuild is not implemented. |
| `/app/audience/preferences` | Audience | Preferences | tenant | `contact.manage` | MISSING | — | No browser API exists yet. Required contract: GET /app/api/preferences (consent and subscription topics). |
| `/app/campaigns` | Campaigns | Campaigns | tenant | `campaign.manage` | MISSING | — | No browser API exists yet. Required contract: GET /app/api/campaigns. |
| `/app/campaigns/new` | Campaigns | New campaign | tenant | `campaign.manage` | MISSING | — | No browser API exists yet. Required contract: POST /app/api/campaigns with preflight, schedule and test actions. |
| `/app/campaigns/:id` | Campaigns | Campaign | tenant | `campaign.manage` | MISSING | — | No browser API exists yet. Required contract: GET /app/api/campaigns/{id} with lifecycle actions and progress. |
| `/app/journeys` | Journeys | Journeys | tenant | `campaign.manage` | MISSING | — | No browser API exists yet. Required contract: GET /app/api/journeys. |
| `/app/journeys/new` | Journeys | New journey | tenant | `campaign.manage` | MISSING | — | No browser API exists yet. Required contract: POST /app/api/journeys. |
| `/app/journeys/:id/builder` | Journeys | Journey builder | tenant | `campaign.manage` | MISSING | — | No browser API exists yet. Required contract: journey graph read/write and publish browser APIs; the durable journey engine is incomplete. |
| `/app/journeys/:id/runs` | Journeys | Journey runs | tenant | `campaign.manage` | MISSING | — | No browser API exists yet. Required contract: journey run history browser API. |
| `/app/analytics/overview` | Analytics | Analytics | tenant | `analytics.read` | PARTIAL | `GET /app/api/dashboard` | Current-window delivery counts come from the dashboard API; historical aggregation has no browser API. |
| `/app/analytics/campaigns` | Analytics | Campaign analytics | tenant | `analytics.read` | MISSING | — | No browser API exists yet. Required contract: campaign analytics browser API. |
| `/app/analytics/journeys` | Analytics | Journey analytics | tenant | `analytics.read` | MISSING | — | No browser API exists yet. Required contract: journey analytics browser API. |
| `/app/analytics/segments` | Analytics | Segment analytics | tenant | `analytics.read` | MISSING | — | No browser API exists yet. Required contract: segment analytics browser API. |
| `/app/analytics/links` | Analytics | Link analytics | tenant | `analytics.read` | MISSING | — | No browser API exists yet. Required contract: link analytics browser API. |
| `/app/deliverability` | Deliverability | Deliverability | tenant | `mail.read` | PARTIAL | `GET /app/api/domains` | Domain claim states come from the domain list; DNS/TLS/PTR checks and trend data have no browser API. |
| `/app/deliverability/domains/:id` | Deliverability | Domain deliverability | tenant | `mail.read` | PARTIAL | `GET /app/api/domains` | Claim state comes from the domain list; evidence detail needs GET /app/api/domains/{id}. |
| `/app/deliverability/ip-pools` | Deliverability | IP pools | tenant | `mail.read` | MISSING | — | No browser API exists yet. Required contract: IP pool and warmup state browser API. |
| `/app/deliverability/alerts` | Deliverability | Alerts | tenant | `mail.read` | MISSING | — | No browser API exists yet. Required contract: deliverability alert feed browser API. |
| `/app/developer/api-keys` | Developer | API keys | tenant | `credential.manage` | MISSING | — | No browser API exists yet. Required contract: GET/POST/DELETE /app/api/api-keys and POST /app/api/api-keys/{id}/rotate. |
| `/app/developer/service-accounts` | Developer | Service accounts | tenant | `credential.manage` | MISSING | — | No browser API exists yet. Required contract: service-account browser API. |
| `/app/developer/smtp` | Developer | SMTP credentials | tenant | `credential.manage` | MISSING | — | No browser API exists yet. Required contract: SMTP credential browser API. |
| `/app/developer/webhooks` | Developer | Webhooks | tenant | `webhook.manage` | MISSING | — | No browser API exists yet. Required contract: webhook subscription and delivery-history browser APIs. |
| `/app/developer/logs` | Developer | Logs | tenant | `credential.manage` | PARTIAL | `GET /app/api/messages` | Accepted message intents come from the message list; an operation log browser API does not exist. |
| `/app/developer/openapi` | Developer | OpenAPI | tenant | `credential.manage` | MISSING | — | No browser API exists yet. Required contract: a same-origin OpenAPI document endpoint under /app/api. |
| `/app/billing/plan` | Billing | Plan | tenant | `billing.read` | PARTIAL | `GET /app/api/dashboard` | The daily quota comes from the dashboard API; subscription and plan detail have no browser API. Live payment actions are disabled. |
| `/app/billing/usage` | Billing | Usage | tenant | `billing.read` | PARTIAL | `GET /app/api/dashboard` | Current-window usage comes from the dashboard API; usage history has no browser API. |
| `/app/billing/invoices` | Billing | Invoices | tenant | `billing.read` | MISSING | — | No browser API exists yet. Required contract: GET /app/api/billing/invoices. |
| `/app/billing/invoices/:id` | Billing | Invoice | tenant | `billing.read` | MISSING | — | No browser API exists yet. Required contract: GET /app/api/billing/invoices/{id}. |
| `/app/billing/payment-methods` | Billing | Payment methods | tenant | `billing.read` | MISSING | — | No browser API exists yet. Required contract: GET/DELETE /app/api/billing/payment-methods; live payment provider actions are disabled by design. |
| `/app/settings/organization` | Settings | Organization | tenant | `session` | IMPLEMENTED | `GET /app/api/context` | Organization identity and memberships come from the browser context API. |
| `/app/settings/team` | Settings | Team | tenant | `session` | IMPLEMENTED | `GET /app/api/team`, `POST /app/api/team/invitations` | Membership and invitations use existing browser APIs; role changes and removals have no browser API. |
| `/app/settings/security` | Settings | Security | tenant | `session` | IMPLEMENTED | `GET /auth/sessions`, `DELETE /auth/sessions/{session_id}`, `POST /auth/logout-all` | Session listing, revocation and sign-out-everywhere use the browser auth APIs; MFA is owned by Keycloak. |
| `/app/settings/sso` | Settings | Single sign-on | tenant | `OWNER/ADMIN` | BLOCKED | — | Tenant SSO configuration is not implemented server-side; sign-in is brokered by Keycloak. |
| `/app/settings/scim` | Settings | SCIM provisioning | tenant | `OWNER/ADMIN` | BLOCKED | — | SCIM provisioning is not implemented server-side. |
| `/app/settings/retention` | Settings | Retention | tenant | `OWNER/ADMIN` | MISSING | — | No browser API exists yet. Required contract: a retention-policy browser API. |
| `/app/settings/audit` | Settings | Audit log | tenant | `OWNER/ADMIN` | MISSING | — | No browser API exists yet. Required contract: an audit read browser API. |
| `/app/settings/integrations` | Settings | Integrations | tenant | `OWNER/ADMIN` | MISSING | — | No browser API exists yet. Required contract: an integration listing/creation browser API. |
| `/app/support` | Support | Support | tenant | `session` | MISSING | — | No browser API exists yet. Required contract: GET/POST /app/api/support/tickets. |
| `/app/support/tickets/:id` | Support | Support ticket | tenant | `session` | MISSING | — | No browser API exists yet. Required contract: GET /app/api/support/tickets/{id}. |
| `/admin/tenants` | Admin | Tenants | platform-admin | `session` | PARTIAL | `GET /app/api/admin/dashboard` | Tenant and user counts come from the admin dashboard API; a tenant listing browser API does not exist. |
| `/admin/deliverability` | Admin | Platform deliverability | platform-admin | `session` | PARTIAL | `GET /app/api/admin/dashboard`, `GET /app/api/admin/provisioning/postal` | Verified-domain counts and provisioning failures come from existing admin APIs; platform DNS/reputation has no browser API. |
| `/admin/abuse` | Admin | Abuse | platform-admin | `session` | MISSING | — | No browser API exists yet. Required contract: an abuse review browser API. |
| `/admin/queues` | Admin | Queues | platform-admin | `session` | PARTIAL | `GET /app/api/admin/dashboard` | Outbox active/failed counts come from the admin dashboard API; queue topology has no browser API. |
| `/admin/reconciliation` | Admin | Reconciliation | platform-admin | `session` | MISSING | — | No browser API exists yet. Required contract: a reconciliation browser API. |
| `/admin/billing` | Admin | Platform billing | platform-admin | `session` | MISSING | — | No browser API exists yet. Required contract: a platform billing browser API; no payment provider is active. |
| `/admin/system` | Admin | System | platform-admin | `session` | IMPLEMENTED | `GET /app/api/admin/dashboard` | Platform counts come from the admin dashboard API. |
| `/admin/audit` | Admin | Platform audit | platform-admin | `session` | MISSING | — | No browser API exists yet. Required contract: a platform audit browser API. |

## Totals

- BLOCKED: 4
- IMPLEMENTED: 8
- MISSING: 41
- PARTIAL: 12
- Routes: 65

## Navigation groups

Overview, Email, Content, Audience, Campaigns, Journeys, Analytics, Deliverability,
Developer, Billing, Settings, Support; the Admin group renders only in the admin
shell after server-proven authority and never appears for ordinary tenants.

## Legacy roots preserved

`/app` (command center), `/app/mail` (webmail), `/app/provisioning`, `/onboarding`,
`/admin` (legacy admin overview), `/admin/provisioning` and every public
authentication route keep their existing root views and behaviour.
