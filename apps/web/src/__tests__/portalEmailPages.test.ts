import { fireEvent, render, screen, waitFor, within } from '@testing-library/vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ appApi: vi.fn(), requireSession: vi.fn(), idempotencyKey: () => 'unit-key' }))
vi.mock('../api', async importOriginal => ({ ...(await importOriginal<typeof import('../api')>()), ...api }))

const reader = { authenticated: true, identity_id: 'i1', tenant_id: 'tenant-one', role: 'READ_ONLY', email: 'r@example.com', expires_at: '2099-01-01T00:00:00Z', csrf_token: 'c', capabilities: ['mail.read'] }
const owner = { ...reader, role: 'OWNER', capabilities: ['*'] }
const messages = Array.from({ length: 3 }, (_, index) => ({ id: `m${index}`, recipient: `user${index}@example.com`, sender: 'hello@example.com', subject: index === 1 ? 'Invoice <b>bold</b>' : `Subject ${index}`, status: index === 2 ? 'failed' : 'delivered', created_at: '2026-09-10T10:00:00Z' }))
const claims = [
  { id: 'claim-1', domain: 'example.com', state: 'VERIFIED', dkim_selector: 'kly1', dkim_version: 1, return_path: 'bounce.example.com', tracking_domain: 'track.example.com', verified_at: '2026-09-01T00:00:00Z', created_at: '2026-08-01T00:00:00Z' },
  { id: 'claim-2', domain: 'pending.example', state: 'DNS_REQUIRED', dkim_selector: 'kly2', dkim_version: 1, return_path: 'bounce.pending.example', tracking_domain: 'track.pending.example', verified_at: null, created_at: '2026-08-02T00:00:00Z' },
]

async function mount(name: string, params: Record<string, string> = {}, session = reader) {
  const { routeByName } = await import('../portal/routes')
  const { pageFor } = await import('../portal/pages')
  const { bindTenant } = await import('../portal/state')
  bindTenant(session.tenant_id)
  const route = routeByName(name)!
  return render(pageFor(route), { props: { route, params, session } })
}
async function apiError(status: number, code: string, requestId = 'req-x') {
  const { ApiError } = await vi.importActual<typeof import('../api')>('../api')
  return new ApiError(status, code, requestId, '')
}

beforeEach(async () => {
  api.appApi.mockReset()
  const { clearTenantState } = await import('../portal/state')
  clearTenantState()
})
afterEach(() => vi.restoreAllMocks())

describe('messages list and detail', () => {
  it('lists messages as text, filters by status and pages with offsets', async () => {
    api.appApi.mockImplementation(async (url: string) => (url.startsWith('/app/api/messages') ? messages : []))
    const { container } = await mount('email-messages')
    expect(await screen.findByRole('table', { name: /messages/i })).toBeTruthy()
    expect(api.appApi).toHaveBeenCalledWith('/app/api/messages?limit=50&offset=0')
    expect(container.querySelector('b')).toBeNull()
    expect(screen.getByText('Invoice <b>bold</b>')).toBeTruthy()
    await fireEvent.update(screen.getByLabelText('Status'), 'failed')
    await waitFor(() => expect(screen.getAllByRole('row').length).toBe(2))
    await fireEvent.update(screen.getByLabelText('Status'), '')
    await fireEvent.update(screen.getByLabelText('Search'), 'user1')
    await waitFor(() => expect(screen.getAllByRole('row').length).toBe(2))
    await fireEvent.update(screen.getByLabelText('Search'), '')
    await fireEvent.click(screen.getByRole('button', { name: 'Next' }))
    await waitFor(() => expect(api.appApi).toHaveBeenCalledWith('/app/api/messages?limit=50&offset=50'))
    expect(screen.getByRole('link', { name: 'Subject 0' }).getAttribute('href')).toBe('/app/email/messages/m0')
  })
  it('shows an empty state and an error with request identifiers', async () => {
    api.appApi.mockResolvedValueOnce([])
    const empty = await mount('email-messages')
    expect(await screen.findByText(/no messages/i)).toBeTruthy()
    empty.unmount()
    api.appApi.mockRejectedValueOnce(await apiError(503, 'delivery_unavailable', 'req-msg'))
    await mount('email-messages')
    expect((await screen.findByRole('alert')).textContent).toContain('req-msg')
  })
  it('renders a message summary with an honest timeline unavailability and no retry controls', async () => {
    api.appApi.mockResolvedValue(messages)
    await mount('email-message', { id: 'm2' })
    expect(await screen.findByText('user2@example.com')).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'Event timeline' })).toBeTruthy()
    expect(screen.getByText(/GET \/app\/api\/messages\/\{id\}/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /retry/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /cancel/i })).toBeNull()
    expect(screen.getByText('m2')).toBeTruthy()
  })
  it('shows not found for an unknown message id', async () => {
    api.appApi.mockResolvedValue(messages)
    await mount('email-message', { id: 'missing' })
    expect(await screen.findByText(/message not found/i)).toBeTruthy()
  })
})

describe('domains', () => {
  it('lists claims with states, hides management actions for readers and links to detail', async () => {
    api.appApi.mockResolvedValue(claims)
    await mount('email-domains')
    expect(await screen.findByText('example.com')).toBeTruthy()
    expect(screen.getByText('dns required')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /claim domain/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /verify/i })).toBeNull()
    expect(screen.getByRole('link', { name: 'pending.example' }).getAttribute('href')).toBe('/app/email/domains/claim-2')
  })
  it('lets owners claim a domain, shows DNS guidance once and verifies with honest failures', async () => {
    api.appApi.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url === '/app/api/domains' && init?.method === 'POST') return { id: 'claim-3', state: 'DNS_REQUIRED', dns: { ownership: { type: 'TXT', name: '_klyrow-verification.new.example', value: 'klyrow=challenge-value' }, spf: { type: 'TXT', name: 'new.example', recommended: 'v=spf1 include:spf.klyrow.com -all' }, dkim: { selector: 'kly3' } } }
      if (url === '/app/api/domains') return claims
      if (url === '/app/api/domains/claim-2/verify') throw await apiError(422, 'dns_ownership_not_verified', 'req-verify')
      throw new Error('unexpected ' + url)
    })
    await mount('email-domains', {}, owner)
    await screen.findByText('example.com')
    await fireEvent.click(screen.getByRole('button', { name: /claim domain/i }))
    const dialog = await screen.findByRole('dialog')
    await fireEvent.update(within(dialog).getByLabelText('Domain'), 'New.Example')
    await fireEvent.submit(within(dialog).getByRole('form'))
    expect(await screen.findByText('_klyrow-verification.new.example')).toBeTruthy()
    expect(screen.getByText('klyrow=challenge-value')).toBeTruthy()
    expect(api.appApi).toHaveBeenCalledWith('/app/api/domains', expect.objectContaining({ method: 'POST', body: JSON.stringify({ domain: 'new.example' }) }))
    const rows = screen.getAllByRole('row')
    const pending = rows.find(row => row.textContent?.includes('pending.example'))!
    await fireEvent.click(within(pending).getByRole('button', { name: /verify/i }))
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toMatch(/TXT record was not found/i)
    expect(alert.textContent).toContain('req-verify')
    expect(screen.queryByText(/verified/i, { selector: '.kp-badge' })?.textContent).not.toContain('pending.example')
  })
  it('shows claim detail with DNS guidance and unavailable evidence sections', async () => {
    api.appApi.mockResolvedValue(claims)
    await mount('email-domain', { id: 'claim-2' })
    expect(await screen.findByRole('heading', { level: 1, name: 'pending.example' })).toBeTruthy()
    expect(screen.getByText('_klyrow-verification.pending.example')).toBeTruthy()
    expect(screen.getByText('kly2._domainkey.pending.example')).toBeTruthy()
    await fireEvent.click(screen.getByRole('tab', { name: 'Evidence' }))
    expect(await screen.findByText(/GET \/app\/api\/domains\/\{id\}/)).toBeTruthy()
    expect(screen.queryByText(/verified/i, { selector: '.kp-badge[data-tone="success"]' })).toBeNull()
  })
})

describe('senders', () => {
  it('lists senders and only offers creation on verified domains to management roles', async () => {
    api.appApi.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url === '/app/api/senders' && init?.method === 'POST') return { id: 's2', status: 'ACTIVE', verified: true }
      if (url === '/app/api/senders') return [{ id: 's1', domain_claim_id: 'claim-1', address: 'hello@example.com', display_name: 'Hello', reply_to: null, stream: 'TRANSACTIONAL', status: 'ACTIVE', verified: true }]
      if (url === '/app/api/domains') return claims
      throw new Error('unexpected ' + url)
    })
    await mount('email-senders', {}, owner)
    expect(await screen.findByText('hello@example.com')).toBeTruthy()
    await fireEvent.click(screen.getByRole('button', { name: /add sender/i }))
    const dialog = await screen.findByRole('dialog')
    const domain = within(dialog).getByLabelText('Verified domain') as HTMLSelectElement
    expect(Array.from(domain.options).map(option => option.value)).toEqual(['claim-1'])
    await fireEvent.update(within(dialog).getByLabelText('Email address'), 'support@example.com')
    await fireEvent.update(within(dialog).getByLabelText('Display name'), 'Support')
    await fireEvent.submit(within(dialog).getByRole('form'))
    await waitFor(() => expect(api.appApi).toHaveBeenCalledWith('/app/api/senders', expect.objectContaining({ method: 'POST' })))
    const body = JSON.parse(String(api.appApi.mock.calls.find(call => call[1]?.method === 'POST')![1].body))
    expect(body).toEqual({ domain_claim_id: 'claim-1', email: 'support@example.com', display_name: 'Support', reply_to: null, stream: 'TRANSACTIONAL' })
  })
  it('hides creation for readers', async () => {
    api.appApi.mockImplementation(async (url: string) => (url === '/app/api/domains' ? claims : []))
    await mount('email-senders')
    expect(await screen.findByText(/no senders/i)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /add sender/i })).toBeNull()
  })
})

describe('inbound', () => {
  it('shows mailbox readiness and lets management activate pending inboxes after confirmation', async () => {
    api.appApi.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url === '/app/api/mailboxes/inbound/activate' && init?.method === 'POST') return { activated_domains: 1, activated_routes: 2 }
      if (url === '/app/api/mailboxes') return [
        { id: 'mb1', address: 'hello@example.com', domain: 'example.com', display_name: 'Hello', sending_enabled: true, receiving_enabled: false, counts: { INBOX: 0 } },
        { id: 'mb2', address: 'ops@example.com', domain: 'example.com', display_name: 'Ops', sending_enabled: true, receiving_enabled: true, counts: { INBOX: 3 } },
      ]
      throw new Error('unexpected ' + url)
    })
    await mount('email-inbound', {}, owner)
    expect(await screen.findByText('ops@example.com')).toBeTruthy()
    await fireEvent.click(screen.getByRole('button', { name: /activate 1 pending/i }))
    await fireEvent.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: /activate/i }))
    await waitFor(() => expect(api.appApi).toHaveBeenCalledWith('/app/api/mailboxes/inbound/activate', expect.objectContaining({ method: 'POST' })))
    expect(await screen.findByText(/activated for 1 domain/i)).toBeTruthy()
    expect(screen.getByRole('link', { name: /open webmail/i }).getAttribute('href')).toBe('/app/mail')
  })
})
