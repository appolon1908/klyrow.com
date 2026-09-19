export type FailureKind = 'forbidden' | 'not-found' | 'unavailable' | 'validation' | 'conflict' | 'session' | 'network' | 'error'

export interface PortalFailure {
  kind: FailureKind
  /** Server detail code (snake_case) or a synthetic `request_failed_<status>` code. */
  code: string
  status?: number
  requestId?: string
  correlationId?: string
}

const CODE_PATTERN = /^[a-z]+(?:_[a-z]+){0,11}$/

/** Only stable snake_case word codes are retained; anything else (including credential-looking strings) is replaced. */
function safeCode(value: string, status?: number): string {
  if (CODE_PATTERN.test(value) && !value.startsWith('kly_')) return value
  return status ? `request_failed_${status}` : 'unexpected_error'
}

interface ApiFailureLike extends Error { status: number; code: string; requestId?: string; correlationId?: string }

/** Structural check so module re-instantiation in tests or lazy chunks never hides a typed failure. */
function isApiFailure(error: unknown): error is ApiFailureLike {
  return error instanceof Error && error.name === 'ApiError' && typeof (error as ApiFailureLike).status === 'number'
}

const SESSION_CODES = new Set(['session_changed', 'session_unavailable', 'authentication_required', 'principal_disabled', 'session_csrf_unavailable'])

export function normalizeFailure(error: unknown): PortalFailure {
  if (isApiFailure(error)) {
    const code = safeCode(error.code, error.status)
    const base = { code, status: error.status, requestId: error.requestId || undefined, correlationId: error.correlationId || undefined }
    if (error.status === 401) return { kind: 'session', ...base }
    if (error.status === 403) return { kind: 'forbidden', ...base }
    if (error.status === 404) return { kind: 'not-found', ...base }
    if (error.status === 409) return { kind: 'conflict', ...base }
    if (error.status === 422 || error.status === 400) return { kind: 'validation', ...base }
    if (error.status === 503 || error.status === 502 || error.status === 504 || error.status === 429) return { kind: 'unavailable', ...base }
    return { kind: 'error', ...base }
  }
  if (error instanceof TypeError) return { kind: 'network', code: 'network_unreachable' }
  if (error instanceof Error) {
    const code = safeCode(error.message)
    if (SESSION_CODES.has(code)) return { kind: 'session', code }
    return { kind: 'error', code }
  }
  return { kind: 'error', code: 'unexpected_error' }
}

const EXPLANATIONS: Record<string, string> = {
  tenant_management_denied: 'This action needs an owner or admin role in the current organization.',
  platform_admin_required: 'Platform administration needs a platform administrator identity.',
  workspace_access_denied: 'Your membership in this organization is no longer active.',
  tenant_suspended: 'This organization is suspended.',
  dns_ownership_not_verified: 'The ownership TXT record was not found in DNS yet. Records can take time to propagate.',
  domain_already_claimed: 'That domain is already claimed by an organization.',
  verified_domain_required: 'Verify the domain before creating senders on it.',
  sender_spoofing_denied: 'The sender address must belong to the selected verified domain.',
  invalid_message_stream: 'Choose a supported message stream.',
  organization_not_found: 'That organization is not available to your account.',
  session_not_found: 'That session no longer exists.',
  delivery_unavailable: 'Delivery is temporarily unavailable.',
  network_unreachable: 'The network request could not be completed.',
  session_changed: 'Your session changed in another tab. Reload to continue.',
  session_unavailable: 'The session service is temporarily unavailable.',
  authentication_required: 'Sign in again to continue.',
  not_found: 'The requested record was not found in this organization.',
}

export function describeFailure(failure: PortalFailure): string {
  if (EXPLANATIONS[failure.code]) return EXPLANATIONS[failure.code]
  switch (failure.kind) {
    case 'forbidden': return 'Your current access does not permit this.'
    case 'not-found': return 'The requested record was not found in this organization.'
    case 'unavailable': return 'This service is temporarily unavailable. Try again shortly.'
    case 'validation': return 'The request was rejected by validation.'
    case 'conflict': return 'The request conflicts with the current server state.'
    case 'session': return 'Your session is no longer valid.'
    case 'network': return 'The network request could not be completed.'
    default: return 'The request failed unexpectedly.'
  }
}

/** Human title for state components. */
export function failureTitle(failure: PortalFailure): string {
  switch (failure.kind) {
    case 'forbidden': return 'Access denied'
    case 'not-found': return 'Not found'
    case 'unavailable': return 'Service unavailable'
    case 'validation': return 'Request rejected'
    case 'conflict': return 'Conflict'
    case 'session': return 'Session expired'
    case 'network': return 'Network problem'
    default: return 'Something went wrong'
  }
}
