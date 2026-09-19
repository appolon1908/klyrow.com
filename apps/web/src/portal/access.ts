import { hasCapability, toOrbitSession, type BrowserSession } from '../session'
import { NAV_GROUPS, portalRoutes, type Availability, type NavGroup, type PortalRoute } from './routes'

/**
 * Platform-admin authority is proven only by the server (GET /app/api/admin/dashboard).
 * `unknown` means the probe has not completed; nothing admin-facing renders until it has.
 */
export type AdminAuthority = 'unknown' | 'proven' | 'denied'

export type AccessDecision =
  | { kind: 'granted' }
  | { kind: 'pending' }
  | { kind: 'signed-out' }
  | { kind: 'forbidden'; reason: 'capability' | 'role' | 'platform-admin' }

export function evaluateAccess(session: BrowserSession, route: PortalRoute, adminAuthority: AdminAuthority): AccessDecision {
  const projection = toOrbitSession(session)
  if (!projection) return { kind: 'signed-out' }
  if (route.audience === 'platform-admin') {
    if (adminAuthority === 'unknown') return { kind: 'pending' }
    return adminAuthority === 'proven' ? { kind: 'granted' } : { kind: 'forbidden', reason: 'platform-admin' }
  }
  if (route.capability && !hasCapability(session, route.capability)) return { kind: 'forbidden', reason: 'capability' }
  if (route.roles && !route.roles.includes(session.role || '')) return { kind: 'forbidden', reason: 'role' }
  return { kind: 'granted' }
}

export interface NavigationItem { path: string; label: string; availability: Availability; name: string }
export interface NavigationGroup { group: NavGroup; items: NavigationItem[] }

/** Navigation reflects access decisions for display only; every page re-evaluates access on entry. */
export function visibleNavigation(session: BrowserSession, adminAuthority: AdminAuthority): NavigationGroup[] {
  if (!toOrbitSession(session)) return []
  const groups: NavigationGroup[] = []
  for (const group of NAV_GROUPS) {
    const items = portalRoutes
      .filter(route => route.nav && route.group === group && evaluateAccess(session, route, adminAuthority).kind === 'granted')
      .map(route => ({ path: route.pattern, label: route.title, availability: route.availability, name: route.name }))
    if (items.length) groups.push({ group, items })
  }
  return groups
}

export function isManagementRole(session: BrowserSession): boolean {
  return ['OWNER', 'ADMIN'].includes(session.role || '')
}
