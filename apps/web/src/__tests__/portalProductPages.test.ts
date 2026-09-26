import { fireEvent, render, screen, waitFor, within } from '@testing-library/vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ appApi: vi.fn(), requireSession: vi.fn(), idempotencyKey: () => 'unit-key' }))
vi.mock('../api', async importOriginal => ({ ...(await importOriginal<typeof import('../api')>()), ...api }))

const owner = { authenticated: true, identity_id: 'i1', tenant_id: 'tenant-one', role: 'OWNER', email: 'owner@example.com', expires_at: '2099-01-01T00:00:00Z', csrf_token: 'c', capabilities: ['*'], session_id: 'sess-current' }
const reader = { ...owner, role: 'READ_ONLY', capabilities: ['mail.read', 'analytics.read', 'billing.read'] }
const dashboard = {
  metrics: { sent_24h: 12, messages_total: 40, quota: 100, delivered: 10, bounced: 1, delivery_rate: 0.9, contacts: 3, campaigns: 0, suppressions: 2, outbox_active: 1, outbox_failed: 0 },
  domains: [], senders: [], recent_messages: [], onboarding: null,
}
const claims = [{ id: 'claim-1', domain: 'example.com', state: 'VERIFIED', dkim_selector: 'kly1', dkim_version: 2, return_path: 'bounce.example.com', tracking_domain: 'track.example.com', verified_at: '2026-09-01T00:00:00Z', created_at: '2026-08-01T00:00:00Z', suspended_at: null }]

async function mount(name: string, params: Record<string, string> = {}, session = owner) {
  const { routeByName } = await import('../portal/routes')
  const { pageFor } = await import('../portal/pages')
  const { bindTenant } = await import('../portal/state')
  bindTenant(session.tenant_id)
  const route = routeByName(name)!
  return { route, ...render(pageFor(route), { props: { route, params, session } }) }
}

beforeEach(async () => {
  api.appApi.mockReset()
  const { clearTenantState } = await import('../portal/state')
  clearTenantState()
})
afterEach(() => vi.restoreAllMocks())

describe('pages without a browser API', () => {
  it('render an honest unavailable state with the missing contract and no mutating controls', async () => {
    const { portalRoutes } = await import('../portal/routes')
    for (const route of portalRoutes.filter(item => item.availability === 'unavailable')) {
      const { unmount, container } = await mount(route.name, route.pattern.includes(':id') ? { id: 'record-1' } : {})
      expect(screen.getAllByText(/not available in this release/i), route.name).toBeTruthy()
      expect(container.textContent, route.name).toContain(route.dependency)
      expect(container.querySelectorAll('button:not([type="button"]), form, input, select, textarea').length, route.name).toBe(0)
      expect(api.appApi, route.name).not.toHaveBeenCalled()
      unmount()
    }
  })
  it('discloses journey engine incompleteness and disabled live payments explicitly', async () => {
    const journeys = await mount('journey-builder', { id: 'j1' })
    expect(screen.getAllByText(/durable journey engine is incomplete/i)).toBeTruthy()
    expect(screen.getAllByText(/waits, branches, webhooks/i)).toBeTruthy()
    journeys.unmount()
    const payments = await mount('billing-payment-methods')
    expect(screen.getAllByText(/live payment provider actions are disabled/i)).toBeTruthy()
    expect(screen.queryByText(/stripe|paypal|crypto/i)).toBeNull()
    payments.unmount()
    api.appApi.mockResolvedValue({
      identity_authority: 'Keycloak',
      browser_session_authority: 'Klyrow BFF',
      sso: { configured: false, mutation_available: false, dependency: 'Governed Keycloak provisioning required.' },
      scim: { configured: false, mutation_available: false, dependency: 'Governed provisioning required.' },
      runtime_certification: 'pending',
      direct_keycloak_writes: false,
    })
    const scim = await mount('settings-scim')
    expect(await screen.findByText(/SCIM provisioning/i)).toBeTruthy()
    expect(screen.getAllByText(/configuration mutation: not available/i).length).toBeGreaterThan(0)
    scim.unmount()
    await mount('audience-preferences')
    expect(screen.getAllByText(/consent.*preference.*suppression/i)).toBeTruthy()
  })
})

describe('analytics, deliverability, developer logs and billing', () => {
  it('uses the authenticated billing BFF for every live billing view without a client tenant selector', async () => {
    const responses: Record<string, unknown> = {
      '/app/api/billing/overview': { subscription: { product: 'Klyrow Email', plan: 'Growth', status: 'ACTIVE', interval: 'MONTHLY' }, outstanding_balance: '0.00', currency: 'USD', wallet_balance: '0.00', most_recent_invoice: null, recent_payments: [], capabilities: [{ key: 'historical_billing', available: true, reason: 'available' }] },
      '/app/api/billing/subscription': { product: 'Klyrow Email', plan: 'Growth', status: 'ACTIVE', interval: 'MONTHLY', price: '29.00', currency: 'USD', usage: [] },
      '/app/api/billing/invoices?offset=0&limit=25': { items: [], limit: 25, offset: 0, has_more: false },
      '/app/api/billing/invoices/invoice-1': { id: 'invoice-1', reference: 'KLY-1', status: 'OPEN', issued_at: '2026-09-01T00:00:00Z', total: '29.00', currency: 'USD', line_items: [] },
      '/app/api/billing/credit-notes': { items: [{ id: 'cn-1', number: 'CN-1', invoice_id: 'invoice-1', amount: '5.00', currency: 'USD', reason: 'Adjustment', created_at: '2026-09-02T00:00:00Z' }] },
      '/app/api/billing/payments': { items: [], limit: 50, offset: 0, has_more: false },
      '/app/api/billing/refunds': { items: [], limit: 50, offset: 0, has_more: false },
      '/app/api/billing/payment-methods': [],
      '/app/api/billing/wallet': { balance: '0.00', currency: 'USD', transactions: [] },
    }
    api.appApi.mockImplementation(async (url: string) => responses[url] ?? responses[url.split('?')[0]])
    const names = ['billing-overview', 'billing-subscription', 'billing-invoices', 'billing-invoice', 'billing-payments', 'billing-refunds', 'billing-payment-methods', 'billing-wallet']
    for (const name of names) {
      const mounted = await mount(name, name === 'billing-invoice' ? { id: 'invoice-1' } : {}, reader)
      await waitFor(() => expect(api.appApi).toHaveBeenCalled())
      mounted.unmount()
    }
    const calls = api.appApi.mock.calls.map(call => String(call[0]))
    expect(calls).toEqual(expect.arrayContaining(Object.keys(responses)))
    expect(calls.some(url => /tenant[_-]?id|organization[_-]?id/.test(url))).toBe(false)
    expect(api.appApi.mock.calls.every(call => !call[1]?.method || call[1].method === 'GET')).toBe(true)
  })

  it('shows real delivery counts only, with source labels and no charts', async () => {
    api.appApi.mockResolvedValue(dashboard)
    const { container } = await mount('analytics-overview', {}, reader)
    expect(await screen.findByText('90.0%')).toBeTruthy()
    expect(container.querySelectorAll('svg, canvas').length).toBe(0)
    expect(screen.getAllByText('live').length).toBeGreaterThan(0)
    expect(screen.getAllByText(/historical aggregation/i)).toBeTruthy()
  })
  it('lists domain claim states on the deliverability page with unavailable checks', async () => {
    api.appApi.mockResolvedValue(claims)
    await mount('deliverability', {}, reader)
    expect(await screen.findByText('example.com')).toBeTruthy()
    expect(screen.getByRole('link', { name: 'example.com' }).getAttribute('href')).toBe('/app/deliverability/domains/claim-1')
    expect(screen.getAllByText(/DNS\/TLS\/PTR checks/i)).toBeTruthy()
  })
  it('uses the message list for developer logs with copy-safe references', async () => {
    api.appApi.mockResolvedValue([{ id: 'm1', recipient: 'a@example.com', sender: 's@example.com', subject: 'Hi', status: 'accepted', created_at: '2026-09-10T10:00:00Z' }])
    await mount('developer-logs')
    expect(await screen.findByText('m1')).toBeTruthy()
    expect(screen.getAllByText(/operation log/i)).toBeTruthy()
    expect(screen.getAllByText(/Authorization: Bearer <redacted>/)).toBeTruthy()
  })
  it('surfaces canonical invoice and credit-note documents through same-origin billing routes', async () => {
    api.appApi.mockImplementation(async (url: string) => {
      if (url === '/app/api/billing/invoices/invoice-1') return { id: 'invoice-1', reference: 'KLY-1', status: 'OPEN', issued_at: '2026-09-01T00:00:00Z', total: '29.00', amount_due: '29.00', currency: 'USD', line_items: [] }
      if (url === '/app/api/billing/credit-notes') return { items: [{ id: 'cn-1', number: 'CN-1', invoice_id: 'invoice-1', amount: '5.00', currency: 'USD', reason: 'Adjustment', created_at: '2026-09-02T00:00:00Z' }] }
      throw new Error('unexpected ' + url)
    })
    await mount('billing-invoice', { id: 'invoice-1' }, reader)
    const invoiceDocument = await screen.findByRole('link', { name: /canonical invoice document/i })
    expect(invoiceDocument.getAttribute('href')).toBe('/app/api/billing/invoices/invoice-1/document')
    const creditNote = await screen.findByRole('link', { name: /credit note CN-1/i })
    expect(creditNote.getAttribute('href')).toBe('/app/api/billing/credit-notes/cn-1/document')
  })

  it('uses billing BFF composition for plan and authoritative usage history without mutation controls', async () => {
    api.appApi.mockImplementation(async (url: string) => {
      if (url === '/app/api/billing/catalog') return { items: [{ code: 'GROWTH', name: 'Growth', features: { messages: 1000 }, price_version: 2, currency: 'USD', billing_cycle: 'MONTHLY', base_amount: '29.00', included_units: 1000, overage_amount: '0.01' }] }
      if (url === '/app/api/billing/subscription') return { product: 'Klyrow Email', plan: 'Growth', status: 'ACTIVE', interval: 'MONTHLY', price: '29.00', currency: 'USD', renews_at: '2026-10-01T00:00:00Z' }
      if (url === '/app/api/billing/capabilities') return { billing_enabled: true, checkout_enabled: false, stripe: { available: false, environment: 'sandbox' }, live_charging: false }
      if (url.startsWith('/app/api/billing/usage/daily')) return { granularity: 'day', unit: 'accepted_message', window_start: '2026-09-01T00:00:00Z', window_end: '2026-10-01T00:00:00Z', items: [{ period_start: '2026-09-20', quantity: 12 }], next_cursor: null }
      if (url === '/app/api/billing/entitlements') return { status: 'ACTIVE', version: 3, entitlements: { messages: { limit: 1000, used: 12, remaining: 988 } } }
      throw new Error('unexpected ' + url)
    })
    const plan = await mount('billing-plan', {}, reader)
    expect(await screen.findByText('Growth')).toBeTruthy()
    expect(document.body.textContent).toMatch(/29[,.]00/)
    expect(screen.getByText(/live charging is not enabled/i)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /pay|upgrade|checkout/i })).toBeNull()
    plan.unmount()

    await mount('billing-usage', {}, reader)
    expect((await screen.findAllByText('12')).length).toBeGreaterThan(0)
    expect(screen.getByText('988')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Daily' }).getAttribute('aria-pressed')).toBe('true')
    expect(api.appApi.mock.calls.some(call => String(call[0]).includes('/app/api/billing/usage/daily'))).toBe(true)
    expect(api.appApi.mock.calls.some(call => /tenant[_-]?id|organization[_-]?id/.test(String(call[0])))).toBe(false)
  })
})

describe('settings', () => {
  it('shows organization identity and memberships from the browser context', async () => {
    api.appApi.mockResolvedValue({ sub: 'u1', identity_id: 'i1', tenant: 'tenant-one', role: 'OWNER', sid: 's1', profile: { email: 'owner@example.com', email_verified: true, display_name: 'Owner', locale: 'en' }, organizations: [{ tenant_id: 'tenant-one', organization_id: 'o1', name: 'Acme', slug: 'acme', role: 'OWNER', enabled: true }] })
    await mount('settings-organization')
    expect((await screen.findAllByText('Acme')).length).toBeGreaterThan(0)
    expect(screen.getAllByText('acme').length).toBeGreaterThan(0)
    expect(screen.getAllByText(/organization edit/i)).toBeTruthy()
  })
  it('lists members, lets owners invite and shows a development token once without persisting it', async () => {
    const setItem = vi.spyOn(Storage.prototype, 'setItem')
    api.appApi.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url === '/app/api/team/invitations' && init?.method === 'POST') return { id: 'inv1', email: 'new@example.com', role: 'DEVELOPER', expires_at: '2026-10-01T00:00:00Z', development_token: 'dev-invite-token-value' }
      if (url === '/app/api/team/invitations') return []
      if (url === '/app/api/team') return [{ user_id: 'u1', email: 'owner@example.com', role: 'OWNER', created_at: '2026-01-01T00:00:00Z' }]
      throw new Error('unexpected ' + url)
    })
    await mount('settings-team')
    expect(await screen.findByText('owner@example.com')).toBeTruthy()
    await fireEvent.click(screen.getByRole('button', { name: /invite member/i }))
    const dialog = await screen.findByRole('dialog')
    await fireEvent.update(within(dialog).getByLabelText('Email address'), 'new@example.com')
    await fireEvent.update(within(dialog).getByLabelText('Role'), 'DEVELOPER')
    await fireEvent.submit(within(dialog).getByRole('form'))
    expect((await screen.findAllByText(/shown once/i)).length).toBeGreaterThan(0)
    expect(document.body.textContent).not.toContain('dev-invite-token-value')
    await fireEvent.click(screen.getByRole('button', { name: 'Reveal' }))
    expect(document.body.textContent).toContain('dev-invite-token-value')
    await fireEvent.click(screen.getByRole('button', { name: /stored it/i }))
    expect(document.body.textContent).not.toContain('dev-invite-token-value')
    expect(setItem).not.toHaveBeenCalled()
    const body = JSON.parse(String(api.appApi.mock.calls.find(call => call[1]?.method === 'POST')![1].body))
    expect(body).toEqual({ email: 'new@example.com', role: 'DEVELOPER', expires_hours: 72 })
  })
  it('hides invitations from readers', async () => {
    api.appApi.mockResolvedValue([])
    await mount('settings-team', {}, reader)
    expect(await screen.findByText(/no members/i)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /invite member/i })).toBeNull()
  })
  it('lists sessions and requires confirmation to revoke one or sign out everywhere', async () => {
    api.appApi.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url === '/auth/sessions') return [
        { id: 'sess-current', current: true, identity_id: 'i1', created_at: '2026-09-10T00:00:00Z', last_seen_at: '2026-09-10T01:00:00Z', expires_at: '2026-09-11T00:00:00Z', revoked_at: null, user_agent_hash: 'ua1', ip_hash: 'ip1' },
        { id: 'sess-other', current: false, identity_id: 'i1', created_at: '2026-09-09T00:00:00Z', last_seen_at: null, expires_at: '2026-09-10T00:00:00Z', revoked_at: null, user_agent_hash: 'ua2', ip_hash: 'ip2' },
      ]
      if (url === '/auth/sessions/sess-other' && init?.method === 'DELETE') return undefined
      if (url === '/auth/logout-all' && init?.method === 'POST') return { logged_out: true }
      throw new Error('unexpected ' + url)
    })
    await mount('settings-security')
    expect(await screen.findByText(/this device/i)).toBeTruthy()
    const rows = screen.getAllByRole('row')
    const other = rows.find(row => row.textContent?.includes('ua2'))!
    await fireEvent.click(within(other).getByRole('button', { name: /revoke/i }))
    await fireEvent.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Revoke session' }))
    await waitFor(() => expect(api.appApi).toHaveBeenCalledWith('/auth/sessions/sess-other', expect.objectContaining({ method: 'DELETE' })))
    expect(screen.getAllByText(/MFA and step-up/i)).toBeTruthy()
    expect(screen.getByRole('button', { name: /sign out everywhere/i })).toBeTruthy()
  })
})

describe('platform administration pages', () => {
  const platform = { tenants: 4, users: 9, messages: 100, outbox_active: 2, outbox_failed: 1, verified_domains: 3, webhooks: 1, usage_events: 50 }
  it('shows counts with unavailable topology for tenants, queues and deliverability', async () => {
    api.appApi.mockImplementation(async (url: string) => (url === '/app/api/admin/dashboard' ? platform : url === '/app/api/admin/provisioning/postal' ? [{ tenant_id: 't9', state: 'BLOCKED', attempts: 3, last_error: 'credential rejected' }] : []))
    const tenants = await mount('admin-tenants')
    expect(await screen.findByText('4')).toBeTruthy()
    expect(screen.getAllByText(/tenant listing/i)).toBeTruthy()
    tenants.unmount()
    const queues = await mount('admin-queues')
    expect(await screen.findByText('2')).toBeTruthy()
    expect(screen.getAllByText(/queue topology/i)).toBeTruthy()
    queues.unmount()
    await mount('admin-deliverability')
    expect(await screen.findByText('t9')).toBeTruthy()
    expect(screen.getByText('credential rejected')).toBeTruthy()
    expect(screen.getByRole('link', { name: /provisioning operations/i }).getAttribute('href')).toBe('/admin/provisioning')
  })
})
