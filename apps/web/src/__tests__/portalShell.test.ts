import { fireEvent, render, screen, waitFor } from '@testing-library/vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ appApi: vi.fn(), requireSession: vi.fn(), getSession: vi.fn(), startSessionSync: vi.fn(), idempotencyKey: () => 'unit-key' }))
vi.mock('../api', async importOriginal => ({ ...(await importOriginal<typeof import('../api')>()), ...api }))

const readOnly = {
  authenticated: true, identity_id: 'identity-one', tenant_id: 'tenant-one', role: 'READ_ONLY', email: 'reader@example.com',
  expires_at: '2099-01-01T00:00:00Z', csrf_token: 'unit-csrf', capabilities: ['mail.read', 'analytics.read', 'billing.read'],
  workspaces: [{ tenant_id: 'tenant-one', role: 'READ_ONLY' }, { tenant_id: 'tenant-two', role: 'OWNER' }],
}
const owner = { ...readOnly, role: 'OWNER', capabilities: ['*'] }
const dashboard = {
  metrics: { sent_24h: 12, messages_total: 40, quota: 100, delivered: 10, bounced: 1, delivery_rate: 0.9, contacts: 3, campaigns: 0, suppressions: 2, outbox_active: 1, outbox_failed: 0 },
  domains: [{ id: 'd1', domain: 'example.com', verified: true }], senders: [{ id: 's1', address: 'hello@example.com', role: 'sender' }],
  recent_messages: [], onboarding: { step: 2, checklist: { profile: true }, completed: false },
}
const platform = { tenants: 4, users: 9, messages: 100, outbox_active: 2, outbox_failed: 1, verified_domains: 3, webhooks: 1, usage_events: 50 }

async function mountAt(path: string, session = readOnly, admin: 'proven' | 'denied' = 'denied') {
  history.replaceState({}, '', path)
  api.requireSession.mockResolvedValue(session)
  api.appApi.mockImplementation(async (url: string) => {
    if (url === '/app/api/admin/dashboard') {
      if (admin === 'proven') return platform
      const { ApiError } = await vi.importActual<typeof import('../api')>('../api')
      throw new ApiError(403, 'platform_admin_required', 'req-admin', '')
    }
    if (url === '/app/api/dashboard') return dashboard
    if (url === '/app/api/onboarding') return dashboard.onboarding
    if (url === '/app/api/provisioning/postal') return { tenant_id: 'tenant-one', state: 'READY', attempts: 1 }
    if (url === '/app/api/domains') return [{ id: 'claim-1', domain: 'example.com', state: 'VERIFIED', dkim_selector: 'kly1', return_path: 'bounce.example.com', tracking_domain: 'track.example.com', verified_at: '2026-09-01T00:00:00Z', created_at: '2026-08-01T00:00:00Z' }]
    if (url === '/app/api/senders') return []
    if (url === '/app/api/context') return { tenant: 'tenant-one', role: session.role, organizations: [{ tenant_id: 'tenant-one', organization_id: 'o1', name: 'Acme', slug: 'acme', role: session.role, enabled: true }, { tenant_id: 'tenant-two', organization_id: 'o2', name: 'Beta', slug: 'beta', role: 'OWNER', enabled: true }] }
    if (url === '/app/api/admin/provisioning/postal') return []
    if (url.startsWith('/app/api/organizations/')) return { ...session, tenant_id: 'tenant-two' }
    throw new Error(`unexpected ${url}`)
  })
  const { resetAdminAuthority } = await import('../portal/adminAuthority')
  resetAdminAuthority()
  const Portal = (await import('../Portal.vue')).default
  return render(Portal)
}

beforeEach(() => { api.appApi.mockReset(); api.requireSession.mockReset() })
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

describe('portal shell', () => {
  it('renders the shell with landmarks, breadcrumbs and role-filtered navigation', async () => {
    await mountAt('/app/overview')
    expect(await screen.findByRole('heading', { level: 1, name: 'Overview' })).toBeTruthy()
    expect(document.querySelector('.kp-skip-link')?.getAttribute('href')).toBe('#kp-main')
    expect(document.getElementById('kp-main')?.tagName).toBe('MAIN')
    const nav = screen.getByRole('navigation', { name: 'Product navigation' })
    expect(nav.textContent).toContain('Messages')
    expect(nav.textContent).not.toContain('Campaigns')
    expect(nav.textContent).not.toContain('Templates')
    expect(screen.queryByRole('navigation', { name: 'Platform administration' })).toBeNull()
    expect(screen.getByRole('navigation', { name: 'Breadcrumb' }).textContent).toContain('Overview')
    expect(screen.getByRole('link', { name: /webmail/i })).toBeTruthy()
  })

  it('shows a forbidden state for a direct unauthorized route without calling its API', async () => {
    await mountAt('/app/campaigns')
    expect((await screen.findByRole('alert')).textContent).toMatch(/does not include the capability/i)
    expect(api.appApi.mock.calls.map(call => call[0])).not.toContain('/app/api/campaigns')
  })

  it('renders an honest unavailable state with the required contract for pages without a browser API', async () => {
    await mountAt('/app/settings/sso', owner)
    expect(await screen.findByText(/not available in this release/i)).toBeTruthy()
    expect(screen.getAllByText(/not implemented server-side/i).length).toBeGreaterThan(0)
    expect(screen.queryByRole('button', { name: /enable/i })).toBeNull()
  })

  it('keeps platform administration isolated until the server proves authority', async () => {
    await mountAt('/admin/system', owner, 'denied')
    expect((await screen.findByRole('alert')).textContent).toMatch(/verified platform administrators/i)
    expect(screen.queryByRole('navigation', { name: 'Platform administration' })).toBeNull()
  })

  it('renders admin surfaces and the separate admin navigation once authority is proven', async () => {
    await mountAt('/admin/system', owner, 'proven')
    expect(await screen.findByRole('heading', { level: 1, name: 'System' })).toBeTruthy()
    expect(screen.getByRole('navigation', { name: 'Platform administration' }).textContent).toContain('Queues')
    expect(await screen.findByText('4', { exact: true })).toBeTruthy()
  })

  it('redirects a navigation group prefix to its first page', async () => {
    const replace = vi.spyOn(history, 'replaceState')
    await mountAt('/app/email')
    await screen.findByRole('heading', { level: 1, name: 'Messages' })
    expect(replace).toHaveBeenCalledWith(null, '', '/app/email/messages')
  })

  it('switches organizations through the BFF and hard-reloads into the new tenant scope', async () => {
    const assign = vi.fn()
    vi.stubGlobal('location', { ...location, origin: location.origin, pathname: '/app/overview', search: '', hash: '', assign })
    const { writeTenantState, readTenantState } = await import('../portal/state')
    await mountAt('/app/overview')
    await screen.findByRole('heading', { level: 1, name: 'Overview' })
    writeTenantState('probe', 1)
    const switcher = await screen.findByLabelText('Organization')
    await fireEvent.update(switcher, 'tenant-two')
    await waitFor(() => expect(api.appApi).toHaveBeenCalledWith('/app/api/organizations/tenant-two/switch', expect.objectContaining({ method: 'POST' })))
    await waitFor(() => expect(assign).toHaveBeenCalledWith('/app/overview'))
    expect(readTenantState('probe')).toBeUndefined()
  })

  it('exposes the mobile navigation toggle with expanded state', async () => {
    await mountAt('/app/overview')
    const toggle = await screen.findByRole('button', { name: 'Open navigation' })
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    await fireEvent.click(toggle)
    expect(screen.getByRole('button', { name: 'Close navigation' }).getAttribute('aria-expanded')).toBe('true')
  })

  it('shows a not-found state for unknown portal paths', async () => {
    await mountAt('/app/email/does-not-exist')
    expect((await screen.findByRole('heading', { level: 1 })).textContent).toMatch(/page not found/i)
  })
})
