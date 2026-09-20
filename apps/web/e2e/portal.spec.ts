import AxeBuilder from '@axe-core/playwright'
import { expect, test, type Page } from '@playwright/test'

const session = {
  authenticated: true, identity_id: 'browser-fixture', tenant_id: 'tenant-one', user_id: 'user-one',
  email: 'owner@example.com', role: 'OWNER', capabilities: ['*'], csrf_token: 'browser-fixture-csrf',
  expires_at: '2099-01-01T00:00:00Z', workspaces: [{ tenant_id: 'tenant-one', role: 'OWNER' }, { tenant_id: 'tenant-two', role: 'ADMIN' }],
}
const reader = { ...session, role: 'READ_ONLY', capabilities: ['mail.read', 'analytics.read', 'billing.read'] }
const dashboard = (tenant: string) => ({
  metrics: { sent_24h: tenant === 'tenant-one' ? 12 : 7, messages_total: 40, quota: 100, delivered: 10, bounced: 1, delivery_rate: 0.9, contacts: 3, campaigns: 0, suppressions: 2, outbox_active: 1, outbox_failed: 0 },
  domains: [], senders: [], recent_messages: [{ id: 'm1', recipient: 'a@example.com', sender: 's@example.com', subject: `Hello ${tenant}`, status: 'delivered', created_at: '2026-09-10T10:00:00Z' }], onboarding: null,
})
const claims = [{ id: 'claim-1', domain: 'example.com', state: 'VERIFIED', dkim_selector: 'kly1', dkim_version: 1, return_path: 'bounce.example.com', tracking_domain: 'track.example.com', verified_at: '2026-09-01T00:00:00Z', created_at: '2026-08-01T00:00:00Z', suspended_at: null }]
const platform = { tenants: 4, users: 9, messages: 100, outbox_active: 2, outbox_failed: 1, verified_domains: 3, webhooks: 1, usage_events: 50 }

async function stubWorkspace(page: Page, options: { session?: typeof session; admin?: boolean; tenant?: string } = {}) {
  const current = options.session || session
  let tenant = options.tenant || current.tenant_id
  await page.route('**/auth/session', route => route.fulfill({ json: { ...current, tenant_id: tenant } }))
  await page.route('**/app/api/admin/dashboard', route => options.admin ? route.fulfill({ json: platform }) : route.fulfill({ status: 403, json: { detail: 'platform_admin_required' }, headers: { 'X-Request-Id': 'req-admin' } }))
  await page.route('**/app/api/admin/provisioning/postal', route => route.fulfill({ json: [] }))
  await page.route('**/app/api/context', route => route.fulfill({ json: { tenant, role: current.role, organizations: [
    { tenant_id: 'tenant-one', organization_id: 'o1', name: 'Acme', slug: 'acme', role: 'OWNER', enabled: true },
    { tenant_id: 'tenant-two', organization_id: 'o2', name: 'Beta', slug: 'beta', role: 'ADMIN', enabled: true },
  ] } }))
  await page.route('**/app/api/dashboard', route => route.fulfill({ json: dashboard(tenant) }))
  await page.route('**/app/api/onboarding', route => route.fulfill({ json: { step: 1, checklist: {}, completed: true } }))
  await page.route('**/app/api/provisioning/postal', route => route.fulfill({ json: { tenant_id: tenant, state: 'READY', attempts: 1 } }))
  await page.route('**/app/api/domains', route => route.fulfill({ json: claims }))
  await page.route('**/app/api/senders', route => route.fulfill({ json: [] }))
  await page.route('**/app/api/messages*', route => route.fulfill({ json: dashboard(tenant).recent_messages }))
  await page.route('**/app/api/team', route => route.fulfill({ json: [{ user_id: 'user-one', email: 'owner@example.com', role: 'OWNER', created_at: '2026-01-01T00:00:00Z' }] }))
  await page.route('**/app/api/organizations/*/switch', route => { tenant = 'tenant-two'; return route.fulfill({ json: { ...current, tenant_id: tenant } }) })
  await page.route('**/app/api/mailboxes', route => route.fulfill({ json: [] }))
}

test('1. signed-out user is redirected from the overview to sign-in with a safe return destination', async ({ page }) => {
  await page.route('**/auth/session', route => route.fulfill({ status: 401, json: { detail: 'session_expired' } }))
  await page.goto('/app/overview')
  await expect(page).toHaveURL(/\/login\?return_to=/)
  expect(new URL(page.url()).searchParams.get('return_to')).toBe('/app/overview')
})

test('2. tenant user navigates permitted pages through the shell without full reloads', async ({ page }) => {
  await stubWorkspace(page, { session: reader })
  await page.goto('/app/overview')
  await expect(page.getByRole('heading', { level: 1, name: 'Overview' })).toBeVisible()
  await page.evaluate(() => { (window as unknown as { __marker: number }).__marker = 42 })
  await page.getByRole('navigation', { name: 'Product navigation' }).getByRole('link', { name: 'Messages' }).click()
  await expect(page).toHaveURL(/\/app\/email\/messages$/)
  await expect(page.getByRole('heading', { level: 1, name: 'Messages' })).toBeVisible()
  expect(await page.evaluate(() => (window as unknown as { __marker: number }).__marker)).toBe(42)
  await expect(page.getByRole('navigation', { name: 'Product navigation' }).getByRole('link', { name: 'Campaigns' })).toHaveCount(0)
  await page.goBack()
  await expect(page.getByRole('heading', { level: 1, name: 'Overview' })).toBeVisible()
})

test('3. tenant user cannot access platform administration directly', async ({ page }) => {
  await stubWorkspace(page)
  await page.goto('/admin/system')
  await expect(page.getByRole('alert')).toContainText('verified platform administrators')
  await expect(page.getByRole('navigation', { name: 'Platform administration' })).toHaveCount(0)
  await expect(page.getByRole('link', { name: 'Queues' })).toHaveCount(0)
})

test('4. platform administrator reaches admin pages with the isolated admin navigation', async ({ page }) => {
  await stubWorkspace(page, { admin: true })
  await page.goto('/admin/system')
  await expect(page.getByRole('heading', { level: 1, name: 'System' })).toBeVisible()
  await expect(page.getByRole('navigation', { name: 'Platform administration' })).toBeVisible()
  await expect(page.getByText('ADMIN', { exact: true })).toBeVisible()
  await page.getByRole('navigation', { name: 'Platform administration' }).getByRole('link', { name: 'Queues' }).click()
  await expect(page.getByRole('heading', { level: 1, name: 'Queues' })).toBeVisible()
  await expect(page.getByText('Outbox active', { exact: true })).toBeVisible()
})

test('5. organization switching reloads into the new tenant scope with fresh data', async ({ page }) => {
  await stubWorkspace(page)
  await page.goto('/app/email/messages')
  await expect(page.getByText('Hello tenant-one')).toBeVisible()
  await page.getByLabel('Organization').selectOption('tenant-two')
  await expect(page).toHaveURL(/\/app\/overview$/)
  await expect(page.getByText('Hello tenant-two')).toBeVisible()
  await expect(page.getByText('Hello tenant-one')).toHaveCount(0)
  expect(await page.evaluate(() => ({ local: Object.keys(localStorage), session: Object.keys(sessionStorage) }))).toEqual({ local: [], session: [] })
})

test('6. webmail remains reachable from the portal and operational', async ({ page }) => {
  await stubWorkspace(page)
  await page.goto('/app/overview')
  await page.getByRole('navigation', { name: 'Product navigation' }).getByRole('link', { name: 'Webmail' }).click()
  await expect(page).toHaveURL(/\/app\/mail$/)
  await expect(page.getByRole('heading', { name: 'Your mail suite is ready to provision' })).toBeVisible()
})

test('7. domain list handles empty, ready and degraded states', async ({ page }) => {
  await stubWorkspace(page)
  await page.route('**/app/api/domains', route => route.fulfill({ json: [] }))
  await page.goto('/app/email/domains')
  await expect(page.getByRole('heading', { name: 'No domains claimed yet' })).toBeVisible()
  await page.route('**/app/api/domains', route => route.fulfill({ json: claims }))
  await page.getByRole('button', { name: 'Refresh' }).click()
  await expect(page.getByRole('link', { name: 'example.com' })).toBeVisible()
  await expect(page.getByText('verified', { exact: true })).toBeVisible()
  await page.route('**/app/api/domains', route => route.fulfill({ status: 503, json: { detail: 'domain_registry_unavailable' }, headers: { 'X-Request-Id': 'req-domains-503' } }))
  await page.getByRole('button', { name: 'Refresh' }).click()
  await expect(page.getByRole('alert')).toContainText('req-domains-503')
  await expect(page.getByRole('alert')).toContainText('domain_registry_unavailable')
})

test('8. one-time credential display never persists and the API keys page exposes no secret controls', async ({ page }) => {
  await stubWorkspace(page)
  await page.route('**/app/api/team/invitations', route => route.fulfill({ status: 201, json: { id: 'inv1', email: 'new@example.com', role: 'DEVELOPER', expires_at: '2026-10-01T00:00:00Z', development_token: 'one-time-invitation-token' } }))
  await page.goto('/app/settings/team')
  await page.getByRole('button', { name: 'Invite member' }).click()
  await page.getByLabel('Email address').fill('new@example.com')
  await page.getByLabel('Role').selectOption('DEVELOPER')
  await page.getByRole('button', { name: 'Send invitation' }).click()
  await expect(page.getByRole('region', { name: 'Development invitation token' }).getByText(/shown once/i)).toBeVisible()
  await expect(page.getByText('one-time-invitation-token')).toHaveCount(0)
  await page.getByRole('button', { name: 'Reveal' }).click()
  await expect(page.getByText('one-time-invitation-token')).toBeVisible()
  await page.getByRole('button', { name: 'I have stored it' }).click()
  await expect(page.getByText('one-time-invitation-token')).toHaveCount(0)
  expect(await page.evaluate(() => ({ local: Object.keys(localStorage), session: Object.keys(sessionStorage) }))).toEqual({ local: [], session: [] })
  expect(page.url()).not.toContain('one-time-invitation-token')
  await page.goto('/app/developer/api-keys')
  await expect(page.getByText('not available in this release')).toBeVisible()
  await expect(page.getByRole('main').locator('form, input, select, textarea')).toHaveCount(0)
})

test('9. SSO and SCIM pages show honest unavailable states', async ({ page }) => {
  await stubWorkspace(page)
  for (const path of ['/app/settings/sso', '/app/settings/scim']) {
    await page.goto(path)
    await expect(page.getByText('not available in this release')).toBeVisible()
    await expect(page.getByText('not implemented server-side').first()).toBeVisible()
    await expect(page.getByRole('button', { name: /enable|configure|save/i })).toHaveCount(0)
  }
})

test('10. billing pages expose no live payment actions or provider branding', async ({ page }) => {
  await stubWorkspace(page)
  for (const path of ['/app/billing/plan', '/app/billing/usage', '/app/billing/invoices', '/app/billing/payment-methods']) {
    await page.goto(path)
    await expect(page.getByRole('main')).not.toContainText(/stripe|paypal|crypto/i)
    await expect(page.getByRole('button', { name: /pay|checkout|upgrade|add card/i })).toHaveCount(0)
  }
  await page.goto('/app/billing/plan')
  await expect(page.getByText('manual or sandbox only').first()).toBeVisible()
})

test('11. media upload transmits bytes and completes only after server confirmation', async ({ page }) => {
  await stubWorkspace(page)
  const png = Buffer.from('89504e470d0a1a0a', 'hex')
  let uploadedBytes = 0
  let completed = false
  await page.route('**/app/api/media?*', route => route.fulfill({ json: { items: [], limit: 24, offset: 0, has_more: false } }))
  await page.route('**/app/api/media/uploads', async route => {
    if (route.request().method() === 'POST') return route.fulfill({ status: 201, json: { id: 'asset-1', version: 1, upload_reference: 'upload-reference-1' } })
    return route.continue()
  })
  await page.route('**/app/api/media/uploads/*', async route => {
    if (route.request().method() === 'PUT') { uploadedBytes = route.request().postDataBuffer()?.length || 0; return route.fulfill({ json: { upload_reference: 'upload-reference-1', size_bytes: uploadedBytes } }) }
    return route.continue()
  })
  await page.route('**/app/api/media/asset-1/complete', async route => {
    completed = true
    return route.fulfill({ json: { id: 'asset-1', status: 'READY', version: 2, original_filename: 'logo.png', size_bytes: uploadedBytes } })
  })
  await page.goto('/app/content/media')
  await page.getByLabel('Choose image').setInputFiles({ name: 'logo.png', mimeType: 'image/png', buffer: png })
  await page.getByRole('button', { name: 'Upload image' }).click()
  await expect.poll(() => uploadedBytes).toBe(png.length)
  await expect.poll(() => completed).toBe(true)
  await expect(page.getByText(/server confirmed READY/i)).toBeVisible()
})

test('11. mobile navigation works by pointer and keyboard', async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 740 })
  await stubWorkspace(page)
  await page.goto('/app/overview')
  await expect(page.locator('body')).not.toHaveCSS('overflow-x', 'scroll')
  const toggle = page.getByRole('button', { name: 'Open navigation' })
  await toggle.click()
  await expect(page.getByRole('button', { name: 'Close navigation' })).toHaveAttribute('aria-expanded', 'true')
  await page.getByRole('navigation', { name: 'Product navigation' }).getByRole('link', { name: 'Domains' }).click()
  await expect(page.getByRole('heading', { level: 1, name: 'Domains' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Open navigation' })).toHaveAttribute('aria-expanded', 'false')
  await page.goto('/app/email/domains')
  await expect(page.getByRole('heading', { level: 1, name: 'Domains' })).toBeVisible()
  await page.keyboard.press('Tab')
  await expect(page.locator('.kp-skip-link')).toBeFocused()
  await page.keyboard.press('Tab')
  await expect(page.getByRole('button', { name: 'Open navigation' })).toBeFocused()
  await page.keyboard.press('Enter')
  await expect(page.getByRole('button', { name: 'Close navigation' })).toHaveAttribute('aria-expanded', 'true')
  await page.keyboard.press('Escape')
  const sendersLink = page.getByRole('navigation', { name: 'Product navigation' }).getByRole('link', { name: 'Senders' })
  await sendersLink.focus()
  await page.keyboard.press('Enter')
  await expect(page.getByRole('heading', { level: 1, name: 'Senders' })).toBeVisible()
})

test('12. error states display request identifiers without secret data', async ({ page }) => {
  await stubWorkspace(page)
  await page.route('**/app/api/messages*', route => route.fulfill({ status: 500, json: { detail: 'kly_notacode0123456789abcdef', extra: 'must-not-render' }, headers: { 'X-Request-Id': 'req-500-abc', 'X-Correlation-Id': 'corr-500-abc' } }))
  await page.goto('/app/email/messages')
  const alert = page.getByRole('alert')
  await expect(alert).toContainText('req-500-abc')
  await expect(alert).toContainText('corr-500-abc')
  await expect(alert).toContainText('request_failed_500')
  await expect(alert).not.toContainText('kly_notacode')
  await expect(page.locator('body')).not.toContainText('must-not-render')
})

for (const viewport of [{ name: 'mobile', width: 360, height: 740 }, { name: 'tablet', width: 768, height: 1024 }, { name: 'desktop', width: 1366, height: 768 }, { name: 'wide', width: 1920, height: 1080 }]) {
  test(`${viewport.name} portal pages are responsive and accessible`, async ({ page }) => {
    await page.setViewportSize(viewport)
    await stubWorkspace(page)
    for (const path of ['/app/overview', '/app/email/domains', '/app/settings/security', '/app/campaigns']) {
      await page.goto(path)
      await expect(page.getByRole('heading', { level: 1 })).toBeVisible()
      await expect(page.locator('body')).not.toHaveCSS('overflow-x', 'scroll')
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true)
      const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']).analyze()
      expect(results.violations, `${viewport.name} ${path}: ${JSON.stringify(results.violations.map(v => ({ id: v.id, nodes: v.nodes.map(n => n.target) })))}`).toEqual([])
    }
  })
}
