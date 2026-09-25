import { hasInvalidPathCharacters, loginLocation, type BrowserSession } from './session'
export type { BrowserSession } from './session'

let csrfToken = ''
let sessionGeneration = 0
let channel: BroadcastChannel | undefined

function invalidateSession() {
  csrfToken = ''
  sessionGeneration += 1
}

export function startSessionSync(): () => void {
  // Cross-tab data is only an invalidation hint. The receiving page must
  // reload its BFF-authorized view; no cookie, identity or CSRF data is shared.
  channel = typeof BroadcastChannel === 'undefined' ? undefined : new BroadcastChannel('klyrow-session')
  if (channel) channel.onmessage = event => {
    if (event.data === 'session-changed') {
      invalidateSession()
      location.reload()
    }
  }
  const restore = (event: PageTransitionEvent) => {
    if (event.persisted) {
      invalidateSession()
      location.reload()
    }
  }
  addEventListener('pageshow', restore)
  return () => {
    channel?.close()
    channel = undefined
    removeEventListener('pageshow', restore)
  }
}

/** Typed browser API failure. The message is the server detail code; identifiers support support requests. */
export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly requestId: string
  readonly correlationId: string

  constructor(status: number, code: string, requestId = '', correlationId = '') {
    super(code)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.requestId = requestId
    this.correlationId = correlationId
  }

  toJSON() {
    return { name: this.name, status: this.status, code: this.code, requestId: this.requestId, correlationId: this.correlationId }
  }
}

async function authenticationFailure(response: Response): Promise<string> {
  const body = await response.json().catch(() => ({})) as { detail?: string } | null
  return body?.detail === 'principal_disabled' ? 'principal_disabled' : 'authentication_required'
}

function redirectAuthenticationFailure(reason: string): never {
  invalidateSession()
  location.assign(reason === 'principal_disabled' ? '/account-disabled' : loginLocation())
  throw new Error(reason)
}

export async function getSession(): Promise<BrowserSession> {
  const generation = sessionGeneration
  csrfToken = ''
  const response = await fetch('/auth/session', {
    credentials: 'same-origin', cache: 'no-store', redirect: 'error',
    headers: { Accept: 'application/json' },
  })
  if (generation !== sessionGeneration) throw new Error('session_changed')
  if (response.status === 401) {
    const reason = await authenticationFailure(response)
    if (reason === 'principal_disabled') redirectAuthenticationFailure(reason)
    return { authenticated: false }
  }
  if (!response.ok) throw new Error('session_unavailable')
  const body = await response.json() as BrowserSession
  if (generation !== sessionGeneration) throw new Error('session_changed')
  csrfToken = body.authenticated ? body.csrf_token || '' : ''
  return body
}

export async function requireSession(): Promise<BrowserSession> {
  const session = await getSession()
  if (!session.authenticated) {
    location.assign(loginLocation())
    throw new Error('authentication_required')
  }
  return session
}

export async function appApi<T>(path: string, init: RequestInit = {}): Promise<T> {
  // Only the browser edge may receive session-bound requests.
  if (!/^\/(app\/api|auth)\//.test(path) || hasInvalidPathCharacters(path) ||
      !/^\/(app\/api|auth)\//.test(new URL(path, location.origin).pathname) ||
      new URL(path, location.origin).origin !== location.origin) throw new Error('browser_api_path_required')
  const method = (init.method || 'GET').toUpperCase()
  const headers = new Headers(init.headers)
  headers.set('Accept', 'application/json')
  if (init.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    if (!csrfToken) await requireSession()
    if (!csrfToken) throw new Error('session_csrf_unavailable')
    headers.set('X-Klyrow-CSRF', csrfToken)
  }
  const response = await fetch(path, { ...init, headers, credentials: 'same-origin', cache: 'no-store', redirect: 'error' })
  if (response.status === 401) {
    redirectAuthenticationFailure(await authenticationFailure(response))
  }
  if (!response.ok) {
    let detail = `request_failed_${response.status}`
    try { detail = String((await response.json() as { detail?: string }).detail || detail) } catch { /* response may be empty */ }
    throw new ApiError(response.status, detail, response.headers.get('X-Request-Id') || '', response.headers.get('X-Correlation-Id') || '')
  }
  if (method === 'POST' && (
    ['/auth/logout', '/auth/logout-all', '/auth/refresh'].includes(path) ||
    /^\/app\/api\/organizations\/[^/]+\/switch$/.test(path)
  )) {
    invalidateSession()
    channel?.postMessage('session-changed')
  }
  if (response.status === 204) return undefined as T
  return await response.json() as T
}

export function idempotencyKey(prefix = 'web') {
  const webCrypto = globalThis.crypto
  if (webCrypto && typeof webCrypto.randomUUID === 'function') {
    return prefix + ':' + webCrypto.randomUUID()
  }
  if (webCrypto && typeof webCrypto.getRandomValues === 'function') {
    const bytes = webCrypto.getRandomValues(new Uint8Array(16))
    bytes[6] = (bytes[6] & 0x0f) | 0x40
    bytes[8] = (bytes[8] & 0x3f) | 0x80
    const hex = Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('')
    const uuid = hex.slice(0, 8) + '-' + hex.slice(8, 12) + '-' + hex.slice(12, 16) + '-' + hex.slice(16, 20) + '-' + hex.slice(20)
    return prefix + ':' + uuid
  }
  return prefix + ':' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 14)
}
