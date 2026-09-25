import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

beforeEach(() => vi.resetModules())
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

const signedIn = { authenticated: true, csrf_token: 'unit-csrf', identity_id: 'identity-one', tenant_id: 'tenant-one' }
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

describe('BFF browser transport', () => {
  it('uses no-store session reads and attaches the server CSRF value on mutations', async () => {
    const fetch = vi.fn().mockResolvedValueOnce(response(signedIn)).mockResolvedValueOnce(response({ status: 'accepted' }, 202))
    vi.stubGlobal('fetch', fetch)
    const { appApi } = await import('../api')
    await appApi('/app/api/email/send', { method: 'POST', headers: { 'Idempotency-Key': 'unit-send' }, body: '{}' })
    expect(fetch.mock.calls[0]).toEqual(['/auth/session', expect.objectContaining({ cache: 'no-store', credentials: 'same-origin', redirect: 'error' })])
    const options = fetch.mock.calls[1][1]
    expect(options.headers.get('X-Klyrow-CSRF')).toBe('unit-csrf')
    expect(options.headers.get('Idempotency-Key')).toBe('unit-send')
    expect(options.credentials).toBe('same-origin')
    expect(options.redirect).toBe('error')
  })
  it('clears a stale CSRF value after a failed session refresh', async () => {
    const fetch = vi.fn().mockResolvedValueOnce(response(signedIn)).mockResolvedValueOnce(response({}, 503))
      .mockResolvedValueOnce(response({ ...signedIn, csrf_token: 'new-unit-csrf' })).mockResolvedValueOnce(response({}))
    vi.stubGlobal('fetch', fetch)
    const { getSession, appApi } = await import('../api')
    await getSession()
    await expect(getSession()).rejects.toThrow('session_unavailable')
    await appApi('/app/api/onboarding', { method: 'PATCH', body: '{}' })
    expect(fetch.mock.calls[3][1].headers.get('X-Klyrow-CSRF')).toBe('new-unit-csrf')
  })
  it('recognizes expired sessions and never retries failed mutations', async () => {
    const fetch = vi.fn().mockResolvedValueOnce(response({}, 401)).mockResolvedValueOnce(response(signedIn))
      .mockResolvedValueOnce(response({ detail: 'mail_send_permission_required' }, 403))
    vi.stubGlobal('fetch', fetch)
    const { getSession, appApi } = await import('../api')
    expect(await getSession()).toEqual({ authenticated: false })
    await expect(appApi('/app/api/email/send', { method: 'POST', body: '{}' })).rejects.toThrow('mail_send_permission_required')
    expect(fetch).toHaveBeenCalledTimes(3)
  })
  it('blocks sending session-bound calls outside the browser edge', async () => {
    const fetch = vi.fn()
    vi.stubGlobal('fetch', fetch)
    const { appApi } = await import('../api')
    for (const path of ['https://example.com/endpoint', '/v1/email/send', '/app/api/../../v1/email/send']) {
      await expect(appApi(path, { method: 'POST' })).rejects.toThrow('browser_api_path_required')
    }
    expect(fetch).not.toHaveBeenCalled()
  })
  it('broadcasts only an invalidation marker after successful logout', async () => {
    const postMessage = vi.fn(), close = vi.fn()
    vi.stubGlobal('BroadcastChannel', class { postMessage = postMessage; close = close; onmessage = null })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(response(signedIn)).mockResolvedValueOnce(response({ logged_out: true })))
    const { appApi, startSessionSync } = await import('../api')
    const stop = startSessionSync()
    await appApi('/auth/logout', { method: 'POST' })
    expect(postMessage).toHaveBeenCalledExactlyOnceWith('session-changed')
    stop()
    expect(close).toHaveBeenCalledOnce()
  })
})


describe('idempotency keys', () => {
  it('falls back to getRandomValues when randomUUID is unavailable', async () => {
    vi.stubGlobal('crypto', {
      getRandomValues: (bytes: Uint8Array) => {
        bytes.forEach((_, index) => { bytes[index] = index })
        return bytes
      },
    })
    const { idempotencyKey } = await import('../api')
    expect(idempotencyKey('webmail-send')).toBe('webmail-send:00010203-0405-4607-8809-0a0b0c0d0e0f')
  })
})
