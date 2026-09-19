import { appApi } from '../api'
import type { AdminAuthority } from './access'
import { normalizeFailure } from './errors'

export interface PlatformMetrics {
  tenants: number
  users: number
  messages: number
  outbox_active: number
  outbox_failed: number
  verified_domains: number
  webhooks: number
  usage_events: number
}

let authority: AdminAuthority = 'unknown'
let metrics: PlatformMetrics | null = null
let pending: Promise<AdminAuthority> | null = null

/**
 * Platform-admin authority is proven only by the server. The admin dashboard
 * read is idempotent and returns 403 platform_admin_required for everyone else.
 */
export async function probeAdminAuthority(): Promise<AdminAuthority> {
  if (authority !== 'unknown') return authority
  if (pending) return pending
  pending = appApi<PlatformMetrics>('/app/api/admin/dashboard')
    .then(result => { metrics = result; authority = 'proven'; return authority })
    .catch((error: unknown) => {
      const failure = normalizeFailure(error)
      authority = failure.kind === 'forbidden' ? 'denied' : 'unknown'
      return authority
    })
    .finally(() => { pending = null })
  return pending
}

export function adminMetrics(): PlatformMetrics | null {
  return metrics
}

export function resetAdminAuthority(): void {
  authority = 'unknown'
  metrics = null
  pending = null
}
