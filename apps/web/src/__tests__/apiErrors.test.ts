import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { describeFailure, normalizeFailure } from '../portal/errors'

beforeEach(() => vi.resetModules())
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

const signedIn = { authenticated: true, csrf_token: 'unit-csrf', identity_id: 'identity-one', tenant_id: 'tenant-one' }
const response = (body: unknown, status = 200, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json', ...headers } })

describe('typed browser API failures', () => {
  it('carries status, detail code and request/correlation identifiers without the response body', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(response({ detail: 'tenant_management_denied', secret: 'must-not-leak' }, 403,
      { 'X-Request-Id': 'req-123', 'X-Correlation-Id': 'corr-456' })))
    const { ApiError, appApi } = await import('../api')
    const failure = await appApi('/app/api/domains').catch((error: unknown) => error)
    expect(failure).toBeInstanceOf(ApiError)
    const typed = failure as InstanceType<typeof ApiError>
    expect(typed.message).toBe('tenant_management_denied')
    expect(typed.status).toBe(403)
    expect(typed.code).toBe('tenant_management_denied')
    expect(typed.requestId).toBe('req-123')
    expect(typed.correlationId).toBe('corr-456')
    expect(JSON.stringify(typed)).not.toContain('must-not-leak')
    expect(signedIn.csrf_token).toBe('unit-csrf')
  })
})

describe('failure normalization for page states', () => {
  it('classifies forbidden, missing, unavailable and unexpected failures with identifiers', async () => {
    const { ApiError } = await import('../api')
    expect(normalizeFailure(new ApiError(403, 'platform_admin_required', 'req-1', ''))).toMatchObject({ kind: 'forbidden', code: 'platform_admin_required', requestId: 'req-1', status: 403 })
    expect(normalizeFailure(new ApiError(404, 'not_found', '', ''))).toMatchObject({ kind: 'not-found' })
    expect(normalizeFailure(new ApiError(503, 'delivery_unavailable', 'req-2', 'corr-2'))).toMatchObject({ kind: 'unavailable', requestId: 'req-2', correlationId: 'corr-2' })
    expect(normalizeFailure(new ApiError(422, 'dns_ownership_not_verified', '', ''))).toMatchObject({ kind: 'validation', code: 'dns_ownership_not_verified' })
    expect(normalizeFailure(new Error('session_changed'))).toMatchObject({ kind: 'session' })
    expect(normalizeFailure(new TypeError('Failed to fetch'))).toMatchObject({ kind: 'network' })
    expect(normalizeFailure('weird')).toMatchObject({ kind: 'error', code: 'unexpected_error' })
  })
  it('renders human explanations for known codes and never echoes credential-like values', async () => {
    const { ApiError } = await import('../api')
    const failure = normalizeFailure(new ApiError(403, 'tenant_management_denied', 'req-9', ''))
    expect(describeFailure(failure)).toMatch(/owner or admin/i)
    const raw = normalizeFailure(new ApiError(500, 'kly_secretvalue0123456789abcdef', 'req-10', ''))
    expect(describeFailure(raw)).not.toContain('kly_secretvalue')
    expect(raw.code).toBe('request_failed_500')
  })
})
