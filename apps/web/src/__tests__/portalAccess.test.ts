import { describe, expect, it } from 'vitest'
import type { BrowserSession } from '../session'
import { evaluateAccess, visibleNavigation } from '../portal/access'
import { matchPortalRoute, portalRoutes } from '../portal/routes'

const base: BrowserSession = {
  authenticated: true, identity_id: 'identity-one', tenant_id: 'tenant-one', role: 'READ_ONLY',
  email: 'user@example.com', expires_at: '2099-01-01T00:00:00Z', csrf_token: 'unit-csrf',
  capabilities: ['mail.read', 'analytics.read', 'billing.read'], workspaces: [{ tenant_id: 'tenant-one', role: 'READ_ONLY' }],
}
const owner: BrowserSession = { ...base, role: 'OWNER', capabilities: ['*'] }
const route = (path: string) => matchPortalRoute(path)!.route

describe('portal access decisions', () => {
  it('requires a valid, unexpired session before anything else', () => {
    expect(evaluateAccess({ authenticated: false }, route('/app/overview'), 'unknown')).toEqual({ kind: 'signed-out' })
    expect(evaluateAccess({ ...base, expires_at: '2000-01-01T00:00:00Z' }, route('/app/overview'), 'unknown')).toEqual({ kind: 'signed-out' })
    expect(evaluateAccess(base, route('/app/overview'), 'unknown')).toEqual({ kind: 'granted' })
  })

  it('enforces route capabilities and management roles from the session, never from navigation', () => {
    expect(evaluateAccess(base, route('/app/email/messages'), 'unknown')).toEqual({ kind: 'granted' })
    expect(evaluateAccess(base, route('/app/campaigns'), 'unknown')).toEqual({ kind: 'forbidden', reason: 'capability' })
    expect(evaluateAccess(base, route('/app/developer/api-keys'), 'unknown')).toEqual({ kind: 'forbidden', reason: 'capability' })
    expect(evaluateAccess(base, route('/app/settings/sso'), 'unknown')).toEqual({ kind: 'forbidden', reason: 'role' })
    expect(evaluateAccess({ ...base, role: 'ADMIN', capabilities: ['tenant.manage'] }, route('/app/settings/sso'), 'unknown')).toEqual({ kind: 'granted' })
    expect(evaluateAccess(owner, route('/app/campaigns'), 'unknown')).toEqual({ kind: 'granted' })
    expect(evaluateAccess({ ...owner, capabilities: [] }, route('/app/campaigns'), 'unknown')).toEqual({ kind: 'forbidden', reason: 'capability' })
  })

  it('never grants platform-admin surfaces without server-proven authority', () => {
    expect(evaluateAccess(owner, route('/admin/system'), 'unknown')).toEqual({ kind: 'pending' })
    expect(evaluateAccess(owner, route('/admin/system'), 'denied')).toEqual({ kind: 'forbidden', reason: 'platform-admin' })
    expect(evaluateAccess(base, route('/admin/system'), 'proven')).toEqual({ kind: 'granted' })
    expect(evaluateAccess({ authenticated: false }, route('/admin/system'), 'proven')).toEqual({ kind: 'signed-out' })
  })

  it('every route is either granted or forbidden for a full-capability owner with proven admin authority', () => {
    for (const item of portalRoutes) expect(evaluateAccess(owner, item, 'proven').kind, item.pattern).toBe('granted')
  })
})

describe('navigation visibility', () => {
  it('lists only groups and items the session may open, keeping admin navigation isolated', () => {
    const readOnly = visibleNavigation(base, 'denied')
    const groups = readOnly.map(group => group.group)
    expect(groups).toEqual(['Overview', 'Email', 'Analytics', 'Deliverability', 'Billing', 'Settings', 'Support'])
    expect(groups).not.toContain('Admin')
    expect(readOnly.find(group => group.group === 'Settings')?.items.map(item => item.path)).toEqual([
      '/app/settings/organization', '/app/settings/team', '/app/settings/security',
    ])
    for (const group of readOnly) for (const item of group.items) expect(item.path).not.toMatch(/:/)
  })
  it('shows every tenant group for an owner and adds the admin group only after proof', () => {
    expect(visibleNavigation(owner, 'unknown').map(group => group.group)).toEqual([
      'Overview', 'Email', 'Content', 'Audience', 'Campaigns', 'Journeys', 'Analytics', 'Deliverability', 'Developer', 'Billing', 'Settings', 'Support',
    ])
    const proven = visibleNavigation(owner, 'proven')
    expect(proven.at(-1)?.group).toBe('Admin')
    expect(proven.at(-1)?.items.map(item => item.path)).toEqual([
      '/admin/tenants', '/admin/deliverability', '/admin/abuse', '/admin/queues', '/admin/reconciliation', '/admin/billing', '/admin/system', '/admin/audit',
    ])
    expect(visibleNavigation({ authenticated: false }, 'proven')).toEqual([])
  })
})
