import { hasInvalidPathCharacters } from '../session'

export const NAV_GROUPS = [
  'Overview', 'Email', 'Content', 'Audience', 'Campaigns', 'Journeys', 'Analytics', 'Deliverability',
  'Developer', 'Billing', 'Settings', 'Support', 'Admin',
] as const
export type NavGroup = typeof NAV_GROUPS[number]
export type Availability = 'implemented' | 'partial' | 'unavailable'
export type PageState = 'loading' | 'empty' | 'ready' | 'degraded' | 'forbidden' | 'unavailable' | 'error'
export type Audience = 'tenant' | 'platform-admin'

export interface PortalRoute {
  /** Stable identifier used to select the page component. */
  name: string
  /** Path pattern; `:param` segments capture one path segment. */
  pattern: string
  group: NavGroup
  title: string
  breadcrumb: string
  audience: Audience
  /** Session capability required to open the page (server authorization remains authoritative). */
  capability?: string
  /** Tenant membership roles required to open the page. */
  roles?: readonly string[]
  availability: Availability
  /** Existing browser API dependency statement, or the missing contract for unavailable pages. */
  dependency: string
  /** Existing browser endpoints the page consumes. */
  apis: readonly string[]
  states: readonly PageState[]
  /** Listed in navigation (parameterised routes never are). */
  nav?: boolean
}

const FULL: readonly PageState[] = ['loading', 'empty', 'ready', 'degraded', 'forbidden', 'unavailable', 'error']
const READY: readonly PageState[] = ['loading', 'empty', 'ready', 'degraded', 'forbidden', 'error']
const MANAGEMENT = ['OWNER', 'ADMIN'] as const

type RouteInput = Omit<PortalRoute, 'states' | 'audience'> & { states?: readonly PageState[]; audience?: Audience }
const tenant = (route: RouteInput): PortalRoute => ({
  audience: 'tenant', states: route.availability === 'implemented' ? READY : FULL, ...route,
})
const admin = (route: RouteInput): PortalRoute => ({
  audience: 'platform-admin', states: route.availability === 'implemented' ? READY : FULL, ...route,
})
const missing = (contract: string) => `No browser API exists yet. Required contract: ${contract}.`

export const portalRoutes: readonly PortalRoute[] = [
  tenant({ name: 'overview', pattern: '/app/overview', group: 'Overview', title: 'Overview', breadcrumb: 'Overview', availability: 'partial', nav: true,
    dependency: 'Readiness, usage, onboarding and provisioning come from existing browser APIs; deliverability, incidents and plan detail have no browser API.',
    apis: ['GET /app/api/dashboard', 'GET /app/api/onboarding', 'GET /app/api/provisioning/postal', 'GET /app/api/domains', 'GET /app/api/senders'] }),

  tenant({ name: 'email-messages', pattern: '/app/email/messages', group: 'Email', title: 'Messages', breadcrumb: 'Messages', capability: 'mail.read', availability: 'implemented', nav: true,
    dependency: 'GET /app/api/messages supports limit/offset paging; status filtering is applied to the loaded page in the browser.', apis: ['GET /app/api/messages'] }),
  tenant({ name: 'email-message', pattern: '/app/email/messages/:id', group: 'Email', title: 'Message', breadcrumb: 'Message', capability: 'mail.read', availability: 'partial',
    dependency: 'Summary fields come from the message list; the event timeline, retry and cancel need GET /app/api/messages/{id}.', apis: ['GET /app/api/messages'] }),
  tenant({ name: 'email-streams', pattern: '/app/email/streams', group: 'Email', title: 'Streams', breadcrumb: 'Streams', capability: 'mail.read', availability: 'unavailable', nav: true,
    dependency: missing('GET/POST /app/api/streams'), apis: [] }),
  tenant({ name: 'email-domains', pattern: '/app/email/domains', group: 'Email', title: 'Domains', breadcrumb: 'Domains', capability: 'mail.read', availability: 'implemented', nav: true,
    dependency: 'Domain claims, creation and DNS ownership verification use existing browser APIs.', apis: ['GET /app/api/domains', 'POST /app/api/domains', 'POST /app/api/domains/{item_id}/verify'] }),
  tenant({ name: 'email-domain', pattern: '/app/email/domains/:id', group: 'Email', title: 'Domain', breadcrumb: 'Domain', capability: 'mail.read', availability: 'partial',
    dependency: 'Claim state and identifiers come from the domain list; SPF/DMARC/PTR/TLS evidence and DKIM history need GET /app/api/domains/{id}.', apis: ['GET /app/api/domains', 'POST /app/api/domains/{item_id}/verify'] }),
  tenant({ name: 'email-senders', pattern: '/app/email/senders', group: 'Email', title: 'Senders', breadcrumb: 'Senders', capability: 'mail.read', availability: 'implemented', nav: true,
    dependency: 'Sender identities and creation use existing browser APIs.', apis: ['GET /app/api/senders', 'POST /app/api/senders', 'GET /app/api/domains'] }),
  tenant({ name: 'email-inbound', pattern: '/app/email/inbound', group: 'Email', title: 'Inbound', breadcrumb: 'Inbound', capability: 'mail.read', availability: 'implemented', nav: true,
    dependency: 'Mailbox readiness and inbound activation use the webmail browser APIs.', apis: ['GET /app/api/mailboxes', 'POST /app/api/mailboxes/inbound/activate'] }),
  tenant({ name: 'email-suppressions', pattern: '/app/email/suppressions', group: 'Email', title: 'Suppressions', breadcrumb: 'Suppressions', capability: 'mail.read', availability: 'implemented', nav: true,
    dependency: 'Tenant-scoped suppression listing and add/remove actions use the authenticated browser BFF.', apis: ['GET /app/api/suppressions', 'POST /app/api/suppressions', 'DELETE /app/api/suppressions/{suppression_id}'] }),

  tenant({ name: 'content-templates', pattern: '/app/content/templates', group: 'Content', title: 'Templates', breadcrumb: 'Templates', capability: 'campaign.manage', availability: 'unavailable', nav: true,
    dependency: missing('GET/POST /app/api/templates'), apis: [] }),
  tenant({ name: 'content-template', pattern: '/app/content/templates/:id', group: 'Content', title: 'Template', breadcrumb: 'Template', capability: 'campaign.manage', availability: 'unavailable',
    dependency: missing('GET /app/api/templates/{id} with version history'), apis: [] }),
  tenant({ name: 'content-builder', pattern: '/app/content/builder/:id', group: 'Content', title: 'Builder', breadcrumb: 'Builder', capability: 'campaign.manage', availability: 'unavailable',
    dependency: missing('template version create/publish/rollback/render browser APIs'), apis: [] }),
  tenant({ name: 'content-media', pattern: '/app/content/media', group: 'Content', title: 'Media', breadcrumb: 'Media', capability: 'campaign.manage', availability: 'implemented', nav: true,
    dependency: 'Tenant-isolated media metadata and lifecycle use the authenticated same-origin Media Library APIs.', apis: ['GET /app/api/media', 'POST /app/api/media/uploads', 'POST /app/api/media/{asset_id}/complete', 'GET /app/api/media/{asset_id}', 'GET /app/api/media/{asset_id}/events', 'POST /app/api/media/{asset_id}/archive', 'DELETE /app/api/media/{asset_id}'] }),
  tenant({ name: 'content-brand', pattern: '/app/content/brand', group: 'Content', title: 'Brand', breadcrumb: 'Brand', capability: 'campaign.manage', availability: 'unavailable', nav: true,
    dependency: missing('a brand settings API (none exists in any audience)'), apis: [] }),

  tenant({ name: 'audience-profiles', pattern: '/app/audience/profiles', group: 'Audience', title: 'Profiles', breadcrumb: 'Profiles', capability: 'contact.manage', availability: 'implemented', nav: true,
    dependency: 'Tenant-scoped profile listing uses the authenticated browser BFF.', apis: ['GET /app/api/profiles'] }),
  tenant({ name: 'audience-profile', pattern: '/app/audience/profiles/:id', group: 'Audience', title: 'Profile', breadcrumb: 'Profile', capability: 'contact.manage', availability: 'implemented',
    dependency: 'Tenant-scoped profile detail includes timeline, consent and preferences from the authenticated browser BFF.', apis: ['GET /app/api/profiles/{id}'] }),
  tenant({ name: 'audience-imports', pattern: '/app/audience/imports', group: 'Audience', title: 'Imports', breadcrumb: 'Imports', capability: 'contact.manage', availability: 'unavailable', nav: true,
    dependency: missing('GET/POST /app/api/imports'), apis: [] }),
  tenant({ name: 'audience-segments', pattern: '/app/audience/segments', group: 'Audience', title: 'Segments', breadcrumb: 'Segments', capability: 'contact.manage', availability: 'unavailable', nav: true,
    dependency: missing('GET/POST /app/api/segments'), apis: [] }),
  tenant({ name: 'audience-segment', pattern: '/app/audience/segments/:id', group: 'Audience', title: 'Segment', breadcrumb: 'Segment', capability: 'contact.manage', availability: 'unavailable',
    dependency: missing('GET /app/api/segments/{id} with preview; membership rebuild is not implemented'), apis: [] }),
  tenant({ name: 'audience-preferences', pattern: '/app/audience/preferences', group: 'Audience', title: 'Preferences', breadcrumb: 'Preferences', capability: 'contact.manage', availability: 'unavailable', nav: true,
    dependency: missing('GET /app/api/preferences (consent and subscription topics)'), apis: [] }),

  tenant({ name: 'campaigns', pattern: '/app/campaigns', group: 'Campaigns', title: 'Campaigns', breadcrumb: 'Campaigns', capability: 'campaign.manage', availability: 'unavailable', nav: true,
    dependency: missing('GET /app/api/campaigns'), apis: [] }),
  tenant({ name: 'campaign-new', pattern: '/app/campaigns/new', group: 'Campaigns', title: 'New campaign', breadcrumb: 'New', capability: 'campaign.manage', availability: 'unavailable',
    dependency: missing('POST /app/api/campaigns with preflight, schedule and test actions'), apis: [] }),
  tenant({ name: 'campaign', pattern: '/app/campaigns/:id', group: 'Campaigns', title: 'Campaign', breadcrumb: 'Campaign', capability: 'campaign.manage', availability: 'unavailable',
    dependency: missing('GET /app/api/campaigns/{id} with lifecycle actions and progress'), apis: [] }),

  tenant({ name: 'journeys', pattern: '/app/journeys', group: 'Journeys', title: 'Journeys', breadcrumb: 'Journeys', capability: 'campaign.manage', availability: 'unavailable', nav: true,
    dependency: missing('GET /app/api/journeys'), apis: [] }),
  tenant({ name: 'journey-new', pattern: '/app/journeys/new', group: 'Journeys', title: 'New journey', breadcrumb: 'New', capability: 'campaign.manage', availability: 'unavailable',
    dependency: missing('POST /app/api/journeys'), apis: [] }),
  tenant({ name: 'journey-builder', pattern: '/app/journeys/:id/builder', group: 'Journeys', title: 'Journey builder', breadcrumb: 'Builder', capability: 'campaign.manage', availability: 'unavailable',
    dependency: missing('journey graph read/write and publish browser APIs; the durable journey engine is incomplete'), apis: [] }),
  tenant({ name: 'journey-runs', pattern: '/app/journeys/:id/runs', group: 'Journeys', title: 'Journey runs', breadcrumb: 'Runs', capability: 'campaign.manage', availability: 'unavailable',
    dependency: missing('journey run history browser API'), apis: [] }),

  tenant({ name: 'analytics-overview', pattern: '/app/analytics/overview', group: 'Analytics', title: 'Analytics', breadcrumb: 'Overview', capability: 'analytics.read', availability: 'partial', nav: true,
    dependency: 'Current-window delivery counts come from the dashboard API; historical aggregation has no browser API.', apis: ['GET /app/api/dashboard'] }),
  tenant({ name: 'analytics-campaigns', pattern: '/app/analytics/campaigns', group: 'Analytics', title: 'Campaign analytics', breadcrumb: 'Campaigns', capability: 'analytics.read', availability: 'unavailable', nav: true,
    dependency: missing('campaign analytics browser API'), apis: [] }),
  tenant({ name: 'analytics-journeys', pattern: '/app/analytics/journeys', group: 'Analytics', title: 'Journey analytics', breadcrumb: 'Journeys', capability: 'analytics.read', availability: 'unavailable', nav: true,
    dependency: missing('journey analytics browser API'), apis: [] }),
  tenant({ name: 'analytics-segments', pattern: '/app/analytics/segments', group: 'Analytics', title: 'Segment analytics', breadcrumb: 'Segments', capability: 'analytics.read', availability: 'unavailable', nav: true,
    dependency: missing('segment analytics browser API'), apis: [] }),
  tenant({ name: 'analytics-links', pattern: '/app/analytics/links', group: 'Analytics', title: 'Link analytics', breadcrumb: 'Links', capability: 'analytics.read', availability: 'unavailable', nav: true,
    dependency: missing('link analytics browser API'), apis: [] }),

  tenant({ name: 'deliverability', pattern: '/app/deliverability', group: 'Deliverability', title: 'Deliverability', breadcrumb: 'Deliverability', capability: 'mail.read', availability: 'partial', nav: true,
    dependency: 'Domain claim states come from the domain list; DNS/TLS/PTR checks and trend data have no browser API.', apis: ['GET /app/api/domains'] }),
  tenant({ name: 'deliverability-domain', pattern: '/app/deliverability/domains/:id', group: 'Deliverability', title: 'Domain deliverability', breadcrumb: 'Domain', capability: 'mail.read', availability: 'partial',
    dependency: 'Claim state comes from the domain list; evidence detail needs GET /app/api/domains/{id}.', apis: ['GET /app/api/domains'] }),
  tenant({ name: 'deliverability-ip-pools', pattern: '/app/deliverability/ip-pools', group: 'Deliverability', title: 'IP pools', breadcrumb: 'IP pools', capability: 'mail.read', availability: 'unavailable', nav: true,
    dependency: missing('IP pool and warmup state browser API'), apis: [] }),
  tenant({ name: 'deliverability-alerts', pattern: '/app/deliverability/alerts', group: 'Deliverability', title: 'Alerts', breadcrumb: 'Alerts', capability: 'mail.read', availability: 'unavailable', nav: true,
    dependency: missing('deliverability alert feed browser API'), apis: [] }),

  tenant({ name: 'developer-api-keys', pattern: '/app/developer/api-keys', group: 'Developer', title: 'API keys', breadcrumb: 'API keys', capability: 'credential.manage', availability: 'unavailable', nav: true,
    dependency: missing('GET/POST/DELETE /app/api/api-keys and POST /app/api/api-keys/{id}/rotate'), apis: [] }),
  tenant({ name: 'developer-service-accounts', pattern: '/app/developer/service-accounts', group: 'Developer', title: 'Service accounts', breadcrumb: 'Service accounts', capability: 'credential.manage', availability: 'unavailable', nav: true,
    dependency: missing('service-account browser API'), apis: [] }),
  tenant({ name: 'developer-smtp', pattern: '/app/developer/smtp', group: 'Developer', title: 'SMTP credentials', breadcrumb: 'SMTP', capability: 'credential.manage', availability: 'unavailable', nav: true,
    dependency: missing('SMTP credential browser API'), apis: [] }),
  tenant({ name: 'developer-webhooks', pattern: '/app/developer/webhooks', group: 'Developer', title: 'Webhooks', breadcrumb: 'Webhooks', capability: 'webhook.manage', availability: 'unavailable', nav: true,
    dependency: missing('webhook subscription and delivery-history browser APIs'), apis: [] }),
  tenant({ name: 'developer-logs', pattern: '/app/developer/logs', group: 'Developer', title: 'Logs', breadcrumb: 'Logs', capability: 'credential.manage', availability: 'partial', nav: true,
    dependency: 'Accepted message intents come from the message list; an operation log browser API does not exist.', apis: ['GET /app/api/messages'] }),
  tenant({ name: 'developer-openapi', pattern: '/app/developer/openapi', group: 'Developer', title: 'OpenAPI', breadcrumb: 'OpenAPI', capability: 'credential.manage', availability: 'unavailable', nav: true,
    dependency: missing('a same-origin OpenAPI document endpoint under /app/api'), apis: [] }),

  tenant({ name: 'billing-overview', pattern: '/app/billing/overview', group: 'Billing', title: 'Billing', breadcrumb: 'Overview', capability: 'billing.read', availability: 'implemented', nav: true,
    dependency: 'Read-only tenant billing overview uses the authenticated browser BFF.', apis: ['GET /app/api/billing/overview'] }),
  tenant({ name: 'billing-plan', pattern: '/app/billing/plan', group: 'Billing', title: 'Plan', breadcrumb: 'Plan', capability: 'billing.read', availability: 'implemented', nav: true,
    dependency: 'Plan catalog, current subscription and provider capability status use the authenticated billing BFF; provider settlement remains separately gated.', apis: ['GET /app/api/billing/catalog', 'GET /app/api/billing/subscription', 'GET /app/api/billing/capabilities'] }),
  tenant({ name: 'billing-subscription', pattern: '/app/billing/subscription', group: 'Billing', title: 'Subscription', breadcrumb: 'Subscription', capability: 'billing.read', availability: 'implemented', nav: true,
    dependency: 'Read-only tenant subscription data uses the authenticated browser BFF.', apis: ['GET /app/api/billing/subscription'] }),
  tenant({ name: 'billing-usage', pattern: '/app/billing/usage', group: 'Billing', title: 'Usage', breadcrumb: 'Usage', capability: 'billing.read', availability: 'implemented', nav: true,
    dependency: 'Bounded daily/monthly history reuses the authoritative tenant usage ledger through authenticated browser BFF routes, with current entitlement context.', apis: ['GET /app/api/billing/usage/daily', 'GET /app/api/billing/usage/monthly', 'GET /app/api/billing/entitlements'] }),
  tenant({ name: 'billing-invoices', pattern: '/app/billing/invoices', group: 'Billing', title: 'Invoices', breadcrumb: 'Invoices', capability: 'billing.read', availability: 'implemented', nav: true,
    dependency: 'Tenant-scoped invoice records use the authenticated browser BFF.', apis: ['GET /app/api/billing/invoices'] }),
  tenant({ name: 'billing-invoice', pattern: '/app/billing/invoices/:id', group: 'Billing', title: 'Invoice', breadcrumb: 'Invoice', capability: 'billing.read', availability: 'implemented',
    dependency: 'Tenant-scoped invoice detail and canonical invoice/credit-note documents use existing authenticated browser billing authorities.', apis: ['GET /app/api/billing/invoices/{invoice_id}', 'GET /app/api/billing/invoices/{invoice_id}/document', 'GET /app/api/billing/credit-notes', 'GET /app/api/billing/credit-notes/{credit_note_id}/document'] }),
  tenant({ name: 'billing-payments', pattern: '/app/billing/payments', group: 'Billing', title: 'Payments', breadcrumb: 'Payments', capability: 'billing.read', availability: 'implemented', nav: true,
    dependency: 'Historical payment records use the authenticated browser BFF.', apis: ['GET /app/api/billing/payments'] }),
  tenant({ name: 'billing-refunds', pattern: '/app/billing/refunds', group: 'Billing', title: 'Refunds', breadcrumb: 'Refunds', capability: 'billing.read', availability: 'implemented', nav: true,
    dependency: 'Historical refund records use the authenticated browser BFF.', apis: ['GET /app/api/billing/refunds'] }),
  tenant({ name: 'billing-payment-methods', pattern: '/app/billing/payment-methods', group: 'Billing', title: 'Payment methods', breadcrumb: 'Payment methods', capability: 'billing.read', availability: 'implemented', nav: true,
    dependency: 'Opaque payment-method references use the authenticated browser BFF; live provider actions remain disabled.', apis: ['GET /app/api/billing/payment-methods'] }),
  tenant({ name: 'billing-wallet', pattern: '/app/billing/wallet', group: 'Billing', title: 'Wallet', breadcrumb: 'Wallet', capability: 'billing.read', availability: 'implemented', nav: true,
    dependency: 'Read-only wallet balance and transactions use the authenticated browser BFF.', apis: ['GET /app/api/billing/wallet'] }),

  tenant({ name: 'settings-organization', pattern: '/app/settings/organization', group: 'Settings', title: 'Organization', breadcrumb: 'Organization', availability: 'implemented', nav: true,
    dependency: 'Organization identity and memberships come from the browser context API.', apis: ['GET /app/api/context'] }),
  tenant({ name: 'settings-team', pattern: '/app/settings/team', group: 'Settings', title: 'Team', breadcrumb: 'Team', availability: 'implemented', nav: true,
    dependency: 'Membership and invitations use existing browser APIs; role changes and removals have no browser API.', apis: ['GET /app/api/team', 'POST /app/api/team/invitations'] }),
  tenant({ name: 'settings-security', pattern: '/app/settings/security', group: 'Settings', title: 'Security', breadcrumb: 'Security', availability: 'implemented', nav: true,
    dependency: 'Session listing, revocation and sign-out-everywhere use the browser auth APIs; MFA is owned by Keycloak.', apis: ['GET /auth/sessions', 'DELETE /auth/sessions/{session_id}', 'POST /auth/logout-all'] }),
  tenant({ name: 'settings-sso', pattern: '/app/settings/sso', group: 'Settings', title: 'Single sign-on', breadcrumb: 'SSO', roles: MANAGEMENT, availability: 'partial', nav: true,
    dependency: 'Read-only enterprise identity readiness is implemented; configuration mutation waits for governed Keycloak/Middleware provisioning.', apis: ['GET /app/api/identity/capabilities'] }),
  tenant({ name: 'settings-scim', pattern: '/app/settings/scim', group: 'Settings', title: 'SCIM provisioning', breadcrumb: 'SCIM', roles: MANAGEMENT, availability: 'partial', nav: true,
    dependency: 'Read-only SCIM readiness is implemented; provisioning mutation waits for governed Keycloak/Middleware provisioning.', apis: ['GET /app/api/identity/capabilities'] }),
  tenant({ name: 'settings-retention', pattern: '/app/settings/retention', group: 'Settings', title: 'Retention', breadcrumb: 'Retention', roles: MANAGEMENT, availability: 'unavailable', nav: true,
    dependency: missing('a retention-policy browser API'), apis: [] }),
  tenant({ name: 'settings-audit', pattern: '/app/settings/audit', group: 'Settings', title: 'Audit log', breadcrumb: 'Audit', roles: MANAGEMENT, availability: 'unavailable', nav: true,
    dependency: missing('an audit read browser API'), apis: [] }),
  tenant({ name: 'settings-integrations', pattern: '/app/settings/integrations', group: 'Settings', title: 'Integrations', breadcrumb: 'Integrations', roles: MANAGEMENT, availability: 'unavailable', nav: true,
    dependency: missing('an integration listing/creation browser API'), apis: [] }),

  tenant({ name: 'support', pattern: '/app/support', group: 'Support', title: 'Support', breadcrumb: 'Support', availability: 'implemented', nav: true,
    dependency: 'Tenant-scoped support tickets are persisted by the authenticated browser BFF; no external provider dispatch is implied.', apis: ['GET /app/api/support/tickets', 'POST /app/api/support/tickets'] }),
  tenant({ name: 'support-ticket', pattern: '/app/support/tickets/:id', group: 'Support', title: 'Support ticket', breadcrumb: 'Ticket', availability: 'implemented',
    dependency: 'Tenant-scoped ticket detail and customer replies use the authenticated browser BFF.', apis: ['GET /app/api/support/tickets/{ticket_id}', 'POST /app/api/support/tickets/{ticket_id}/messages'] }),

  admin({ name: 'admin-tenants', pattern: '/admin/tenants', group: 'Admin', title: 'Tenants', breadcrumb: 'Tenants', availability: 'partial', nav: true,
    dependency: 'Tenant and user counts come from the admin dashboard API; a tenant listing browser API does not exist.', apis: ['GET /app/api/admin/dashboard'] }),
  admin({ name: 'admin-deliverability', pattern: '/admin/deliverability', group: 'Admin', title: 'Platform deliverability', breadcrumb: 'Deliverability', availability: 'partial', nav: true,
    dependency: 'Verified-domain counts and provisioning failures come from existing admin APIs; platform DNS/reputation has no browser API.', apis: ['GET /app/api/admin/dashboard', 'GET /app/api/admin/provisioning/postal'] }),
  admin({ name: 'admin-abuse', pattern: '/admin/abuse', group: 'Admin', title: 'Abuse', breadcrumb: 'Abuse', availability: 'implemented', nav: true,
    dependency: 'Platform-admin abuse review reuses the canonical delivery-control authority for alerts, evaluation, suspension and release; every mutation requires CSRF and is audited.',
    apis: ['GET /app/api/admin/abuse', 'POST /app/api/admin/abuse/evaluate', 'POST /app/api/admin/abuse/suspensions', 'POST /app/api/admin/abuse/suspensions/{item_id}/release', 'POST /app/api/admin/abuse/alerts/{alert_id}/state'] }),
  admin({ name: 'admin-queues', pattern: '/admin/queues', group: 'Admin', title: 'Queues', breadcrumb: 'Queues', availability: 'partial', nav: true,
    dependency: 'Outbox active/failed counts come from the admin dashboard API; queue topology has no browser API.', apis: ['GET /app/api/admin/dashboard'] }),
  admin({ name: 'admin-reconciliation', pattern: '/admin/reconciliation', group: 'Admin', title: 'Reconciliation', breadcrumb: 'Reconciliation', availability: 'implemented', nav: true,
    dependency: 'Read-only platform and billing reconciliation evidence comes from durable authorities; the bounded platform check records drift and never auto-corrects history.',
    apis: ['GET /app/api/admin/reconciliation', 'GET /app/api/admin/reconciliation/billing', 'GET /app/api/admin/reconciliation/{run_id}', 'POST /app/api/admin/reconciliation'] }),
  admin({ name: 'admin-billing', pattern: '/admin/billing', group: 'Admin', title: 'Platform billing', breadcrumb: 'Billing', availability: 'implemented', nav: true,
    dependency: 'Cross-tenant billing reads are derived from canonical Klyrow billing tables; provider secrets are never returned and dunning remains fail-closed behind its capability gate.',
    apis: ['GET /app/api/admin/billing/overview', 'GET /app/api/admin/billing/subscriptions', 'POST /app/api/admin/billing/dunning'] }),
  admin({ name: 'admin-operations', pattern: '/admin/operations', group: 'Admin', title: 'Operations Center', breadcrumb: 'Operations', availability: 'implemented', nav: true,
    dependency: 'Unified read-only health across Users, Email, Billing and Middleware.', apis: ['GET /app/api/admin/observability/operations-center', 'GET /app/api/admin/observability/users', 'GET /app/api/admin/observability/billing', 'GET /app/api/admin/observability/system'] }),
  admin({ name: 'admin-observability', pattern: '/admin/observability', group: 'Admin', title: 'Observability', breadcrumb: 'Observability', availability: 'implemented', nav: true,
    dependency: 'Read-only Webmail/Postal operational projection from the authenticated browser BFF; cross-system commands remain Caddy → Kong → Middleware.', apis: ['GET /app/api/admin/observability/webmail-postal', 'GET /app/api/admin/observability/webmail-postal/slo', 'GET /app/api/admin/observability/webmail-postal/incidents', 'GET /app/api/admin/observability/webmail-postal/architecture', 'GET /app/api/admin/observability/webmail-postal/traces/{correlation_id}'] }),
  admin({ name: 'admin-system', pattern: '/admin/system', group: 'Admin', title: 'System', breadcrumb: 'System', availability: 'implemented', nav: true,
    dependency: 'Platform counts come from the admin dashboard API.', apis: ['GET /app/api/admin/dashboard'] }),
  admin({ name: 'admin-audit', pattern: '/admin/audit', group: 'Admin', title: 'Platform audit', breadcrumb: 'Audit', availability: 'implemented', nav: true,
    dependency: 'The platform audit surface is read-only, filterable and cursor-paginated over the canonical application audit log.',
    apis: ['GET /app/api/admin/audit'] }),
]

export interface PortalMatch { route: PortalRoute; params: Record<string, string> }

function segments(path: string): string[] {
  return path.split('/').filter(Boolean)
}

/** Match a pathname against the portal route table. API and legacy root paths never match. */
export function matchPortalRoute(pathname: string): PortalMatch | null {
  if (hasInvalidPathCharacters(pathname) || pathname.startsWith('/app/api')) return null
  const parts = segments(pathname)
  for (const route of portalRoutes) {
    const pattern = segments(route.pattern)
    if (pattern.length !== parts.length) continue
    const params: Record<string, string> = {}
    let matched = true
    for (let index = 0; index < pattern.length; index += 1) {
      const expected = pattern[index]
      if (expected.startsWith(':')) {
        let value = parts[index]
        try { value = decodeURIComponent(value) } catch { matched = false; break }
        params[expected.slice(1)] = value
      } else if (expected !== parts[index]) { matched = false; break }
    }
    if (matched) return { route, params }
  }
  return null
}

/** Navigation group prefixes (e.g. `/app/email`) open the first listed page in that group. */
export function groupIndexPath(pathname: string): string | null {
  const parts = segments(pathname)
  if (parts.length !== 2) return null
  const prefix = '/' + parts.join('/') + '/'
  const first = portalRoutes.find(route => route.nav && route.pattern.startsWith(prefix))
  return first ? first.pattern : null
}

/** Same-origin portal destination or the overview fallback; never an API, auth or foreign URL. */
export function safePortalPath(value: string, fallback = '/app/overview'): string {
  if (!value || value.length > 2048 || !value.startsWith('/') || value.startsWith('//') || hasInvalidPathCharacters(value)) return fallback
  const target = new URL(value, location.origin)
  if (target.origin !== location.origin) return fallback
  const path = target.pathname
  if (path.startsWith('/app/api') || path.startsWith('/auth')) return fallback
  if (!matchPortalRoute(path) && !groupIndexPath(path)) return fallback
  return target.pathname + target.search + target.hash
}

export function routeByName(name: string): PortalRoute | undefined {
  return portalRoutes.find(route => route.name === name)
}
