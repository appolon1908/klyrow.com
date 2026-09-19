import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

beforeEach(() => vi.resetModules())
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

describe('tenant-bound portal state', () => {
  it('drops every cached entry when the bound organization changes', async () => {
    const { bindTenant, clearTenantState, readTenantState, writeTenantState } = await import('../portal/state')
    bindTenant('tenant-one')
    writeTenantState('dashboard', { metrics: { sent_24h: 3 } })
    expect(readTenantState('dashboard')).toEqual({ metrics: { sent_24h: 3 } })
    bindTenant('tenant-one')
    expect(readTenantState('dashboard')).toBeDefined()
    bindTenant('tenant-two')
    expect(readTenantState('dashboard')).toBeUndefined()
    writeTenantState('dashboard', { metrics: { sent_24h: 9 } })
    clearTenantState()
    expect(readTenantState('dashboard')).toBeUndefined()
  })
  it('never touches browser storage', async () => {
    const setItem = vi.spyOn(Storage.prototype, 'setItem')
    const { bindTenant, writeTenantState } = await import('../portal/state')
    bindTenant('tenant-one')
    writeTenantState('secret-like', { key: 'kly_value' })
    expect(setItem).not.toHaveBeenCalled()
    expect(localStorage.length).toBe(0)
    expect(sessionStorage.length).toBe(0)
  })
})

describe('platform-admin authority probe', () => {
  const signedIn = { authenticated: true, csrf_token: 'unit-csrf', identity_id: 'identity-one', tenant_id: 'tenant-one', expires_at: '2099-01-01T00:00:00Z' }
  const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
  it('is proven only by a successful admin dashboard read and denied on 403', async () => {
    const fetch = vi.fn().mockResolvedValueOnce(response({ tenants: 1, users: 2 })).mockResolvedValueOnce(response({ detail: 'platform_admin_required' }, 403))
    vi.stubGlobal('fetch', fetch)
    const { probeAdminAuthority, resetAdminAuthority } = await import('../portal/adminAuthority')
    expect(await probeAdminAuthority()).toBe('proven')
    expect(await probeAdminAuthority()).toBe('proven')
    expect(fetch).toHaveBeenCalledTimes(1)
    resetAdminAuthority()
    expect(await probeAdminAuthority()).toBe('denied')
    expect(signedIn.authenticated).toBe(true)
  })
  it('stays unknown when the probe fails for another reason', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(response({}, 503)))
    const { probeAdminAuthority } = await import('../portal/adminAuthority')
    expect(await probeAdminAuthority()).toBe('unknown')
  })
})

describe('toast notifications', () => {
  it('queues, auto-identifies and dismisses notifications', async () => {
    const { dismissToast, notify, toasts } = await import('../portal/toasts')
    const id = notify('success', 'Saved')
    expect(toasts.map(toast => toast.message)).toEqual(['Saved'])
    dismissToast(id)
    expect(toasts.length).toBe(0)
  })
})

describe('in-app portal navigation', () => {
  it('uses history for portal destinations and hard navigation for legacy roots', async () => {
    const assign = vi.fn()
    vi.stubGlobal('location', { ...location, origin: 'http://localhost', pathname: '/app/overview', search: '', hash: '', assign })
    const push = vi.spyOn(history, 'pushState')
    const { currentLocation, navigate } = await import('../portal/router')
    navigate('/app/email/domains?tab=dns')
    expect(push).toHaveBeenCalledWith(null, '', '/app/email/domains?tab=dns')
    expect(currentLocation.path).toBe('/app/email/domains')
    expect(currentLocation.search).toBe('?tab=dns')
    navigate('/app/mail')
    expect(assign).toHaveBeenCalledWith('/app/mail')
    navigate('https://evil.example/app/overview')
    expect(assign).toHaveBeenCalledTimes(1)
    expect(currentLocation.path).toBe('/app/overview')
  })
})
