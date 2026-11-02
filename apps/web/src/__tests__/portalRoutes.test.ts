import { describe, expect, it } from 'vitest'
import { browserRoute, routeManifest } from '../routeManifest'
import { NAV_GROUPS, groupIndexPath, matchPortalRoute, portalRoutes, safePortalPath } from '../portal/routes'

const REQUIRED_TENANT_ROUTES = [
  '/app/overview',
  '/app/email/messages', '/app/email/messages/:id', '/app/email/streams', '/app/email/domains', '/app/email/domains/:id',
  '/app/email/senders', '/app/email/inbound', '/app/email/suppressions',
  '/app/content/templates', '/app/content/templates/:id', '/app/content/builder/:id', '/app/content/media', '/app/content/brand',
  '/app/audience/profiles', '/app/audience/profiles/:id', '/app/audience/imports', '/app/audience/segments', '/app/audience/segments/:id', '/app/audience/preferences',
  '/app/campaigns', '/app/campaigns/new', '/app/campaigns/:id',
  '/app/journeys', '/app/journeys/new', '/app/journeys/:id/builder', '/app/journeys/:id/runs',
  '/app/analytics/overview', '/app/analytics/campaigns', '/app/analytics/journeys', '/app/analytics/segments', '/app/analytics/links',
  '/app/deliverability', '/app/deliverability/domains/:id', '/app/deliverability/ip-pools', '/app/deliverability/alerts',
  '/app/developer/api-keys', '/app/developer/service-accounts', '/app/developer/smtp', '/app/developer/webhooks', '/app/developer/logs', '/app/developer/openapi',
  '/app/billing/overview', '/app/billing/plan', '/app/billing/usage', '/app/billing/subscription', '/app/billing/invoices', '/app/billing/invoices/:id', '/app/billing/payments', '/app/billing/refunds', '/app/billing/payment-methods', '/app/billing/wallet',
  '/app/settings/organization', '/app/settings/team', '/app/settings/security', '/app/settings/sso', '/app/settings/scim',
  '/app/settings/retention', '/app/settings/audit', '/app/settings/integrations',
  '/app/support', '/app/support/tickets/:id',
]
const REQUIRED_ADMIN_ROUTES = [
  '/admin/tenants', '/admin/deliverability', '/admin/abuse', '/admin/queues', '/admin/reconciliation', '/admin/billing',
  '/admin/operations', '/admin/observability', '/admin/system', '/admin/audit',
]

describe('portal route table', () => {
  it('declares every Phase 10 tenant and platform-admin route with complete metadata', () => {
    const patterns = portalRoutes.map(route => route.pattern)
    for (const pattern of [...REQUIRED_TENANT_ROUTES, ...REQUIRED_ADMIN_ROUTES]) expect(patterns).toContain(pattern)
    expect(new Set(patterns).size).toBe(patterns.length)
    expect(new Set(portalRoutes.map(route => route.name)).size).toBe(portalRoutes.length)
    for (const route of portalRoutes) {
      expect(route.title.length).toBeGreaterThan(0)
      expect(route.breadcrumb.length).toBeGreaterThan(0)
      expect(NAV_GROUPS).toContain(route.group)
      expect(['implemented', 'partial', 'unavailable']).toContain(route.availability)
      expect(route.dependency.length).toBeGreaterThan(0)
      expect(route.states).toContain('loading')
      expect(route.states).toContain('error')
      expect(route.states).toContain('ready')
      expect(route.states).toContain('forbidden')
      if (route.availability !== 'implemented') expect(route.states).toContain('unavailable')
      expect(route.pattern.startsWith(route.audience === 'platform-admin' ? '/admin/' : '/app/')).toBe(true)
      if (route.audience === 'platform-admin') expect(route.group).toBe('Admin')
      else expect(route.group).not.toBe('Admin')
      for (const api of route.apis) expect(api).toMatch(/^(GET|POST|PUT|PATCH|DELETE) \/(app\/api|auth)\//)
    }
  })

  it('marks the completed admin operations surfaces as real browser-backed pages', () => {
    for (const name of ['admin-abuse', 'admin-reconciliation', 'admin-billing', 'admin-audit']) {
      const route = portalRoutes.find(item => item.name === name)
      expect(route, name).toBeTruthy()
      expect(route?.availability, name).toBe('implemented')
      expect(route?.apis.length, name).toBeGreaterThan(0)
      expect(route?.apis.every(api => api.startsWith('GET /app/api/admin/') || api.startsWith('POST /app/api/admin/'))).toBe(true)
    }
  })

  it('matches concrete paths, extracts parameters and rejects API or foreign paths', () => {
    expect(matchPortalRoute('/app/overview')?.route.name).toBe('overview')
    const domain = matchPortalRoute('/app/email/domains/claim-1')
    expect(domain?.route.pattern).toBe('/app/email/domains/:id')
    expect(domain?.params).toEqual({ id: 'claim-1' })
    expect(matchPortalRoute('/app/journeys/j1/builder')?.params).toEqual({ id: 'j1' })
    expect(matchPortalRoute('/app/journeys/new')?.route.pattern).toBe('/app/journeys/new')
    expect(matchPortalRoute('/app/campaigns/new')?.route.pattern).toBe('/app/campaigns/new')
    expect(matchPortalRoute('/app/email/messages/')?.route.pattern).toBe('/app/email/messages')
    expect(matchPortalRoute('/app/api/dashboard')).toBeNull()
    expect(matchPortalRoute('/app/mail')).toBeNull()
    expect(matchPortalRoute('/app')).toBeNull()
    expect(matchPortalRoute('/admin')).toBeNull()
    expect(matchPortalRoute('/admin/provisioning')).toBeNull()
    expect(matchPortalRoute('/app/email/domains/a/b')).toBeNull()
    expect(matchPortalRoute('/app/email/domains/%2e%2e')?.params).toEqual({ id: '..' })
  })

  it('resolves navigation group index paths to the first visible page', () => {
    expect(groupIndexPath('/app/email')).toBe('/app/email/messages')
    expect(groupIndexPath('/app/settings')).toBe('/app/settings/organization')
    expect(groupIndexPath('/app/billing')).toBe('/app/billing/overview')
    expect(groupIndexPath('/app/unknown')).toBeNull()
  })

  it('accepts only same-origin portal destinations for in-app navigation', () => {
    for (const path of ['/app/overview', '/app/email/domains/x?tab=dns#records', '/admin/system']) expect(safePortalPath(path)).toBe(path)
    for (const path of ['https://example.com/app/overview', '//evil.example/app', '/app/api/dashboard', '/auth/logout', 'javascript:alert(1)', '/app/\\overview', '']) {
      expect(safePortalPath(path)).toBe('/app/overview')
    }
  })

  it('keeps billing routes tenant-scoped and read-only', () => {
    const billing = portalRoutes.filter(route => route.group === 'Billing')
    expect(billing.every(route => route.audience === 'tenant' && route.capability === 'billing.read')).toBe(true)
    expect(billing.flatMap(route => route.apis).some(api => /\b(POST|PUT|PATCH|DELETE)\b/.test(api))).toBe(false)
    expect(billing.flatMap(route => route.apis).every(api => api.startsWith('GET /app/api/billing/') || api === 'GET /app/api/dashboard')).toBe(true)
  })
})

describe('root route manifest integration', () => {
  it('mounts the Portal root for every portal route while preserving legacy roots', () => {
    for (const route of portalRoutes) {
      const concrete = route.pattern.replace(/:[a-z]+/g, 'x')
      expect(browserRoute(concrete)?.view, concrete).toBe('Portal')
      expect(browserRoute(concrete)?.access).toBe('session')
    }
    expect(browserRoute('/app')?.view).toBe('Dashboard')
    expect(browserRoute('/app/mail/thread')?.view).toBe('Webmail')
    expect(browserRoute('/app/provisioning')?.view).toBe('Provisioning')
    expect(browserRoute('/admin')?.view).toBe('AdminDashboard')
    expect(browserRoute('/admin/provisioning')?.view).toBe('Provisioning')
    expect(browserRoute('/onboarding')?.view).toBe('Onboarding')
    expect(browserRoute('/login')?.view).toBe('App')
    for (const entry of routeManifest.filter(route => route.view === 'Portal')) {
      for (const state of ['loading', 'error', 'empty', 'ready', 'forbidden', 'unavailable', 'degraded']) expect(entry.states).toContain(state)
    }
  })
})
