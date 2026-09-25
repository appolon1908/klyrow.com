import { paths } from './auth'

export const rootViews = ['App', 'Dashboard', 'AdminDashboard', 'Onboarding', 'Provisioning', 'Webmail', 'Portal'] as const
export type RootView = typeof rootViews[number]

export interface BrowserRoute {
  path: string
  descendants?: boolean
  view: RootView
  access: 'public' | 'session'
  states: readonly string[]
}

// Product portal prefixes mount the typed portal router (src/portal/routes.ts),
// which owns the page-level route table, access metadata and honest
// unavailable states for pages without a browser API.
const PORTAL_STATES = ['loading', 'error', 'empty', 'ready', 'forbidden', 'unavailable', 'degraded'] as const
const portalPrefixes = [
  '/app/overview', '/app/email', '/app/content', '/app/audience', '/app/campaigns', '/app/journeys', '/app/analytics',
  '/app/deliverability', '/app/developer', '/app/billing', '/app/settings', '/app/support',
  '/admin/tenants', '/admin/deliverability', '/admin/abuse', '/admin/queues', '/admin/reconciliation', '/admin/billing',
  '/admin/operations', '/admin/observability', '/admin/system', '/admin/audit',
] as const

// Ordered before the general product prefixes. These are current root views,
// not a claim that every intended product page has been implemented.
export const routeManifest: readonly BrowserRoute[] = [
  ...portalPrefixes.map(path => ({ path, descendants: true, view: 'Portal' as const, access: 'session' as const, states: PORTAL_STATES })),
  { path: '/app/provisioning', view: 'Provisioning', access: 'session', states: ['loading', 'error', 'empty', 'ready'] },
  { path: '/admin/provisioning', view: 'Provisioning', access: 'session', states: ['loading', 'error', 'empty', 'ready'] },
  { path: '/app/mail', descendants: true, view: 'Webmail', access: 'session', states: ['loading', 'error', 'empty', 'ready'] },
  { path: '/admin', descendants: true, view: 'AdminDashboard', access: 'session', states: ['loading', 'error', 'ready'] },
  { path: '/onboarding', view: 'Onboarding', access: 'session', states: ['loading', 'error', 'ready'] },
  { path: '/app', descendants: true, view: 'Dashboard', access: 'session', states: ['loading', 'error', 'ready'] },
  { path: '/', view: 'App', access: 'public', states: ['ready', 'validation', 'error'] },
  ...paths.map(path => ({ path: '/' + path, view: 'App' as const, access: 'public' as const, states: ['ready', 'validation', 'error'] })),
]

export function browserRoute(path: string): BrowserRoute | undefined {
  // API requests belong to Nginx/BFF, never to a product shell.
  if (path === '/app/api' || path.startsWith('/app/api/') ||
      path === '/auth' || path.startsWith('/auth/')) return undefined
  return routeManifest.find(route => path === route.path || (route.descendants && path.startsWith(route.path + '/')))
}
