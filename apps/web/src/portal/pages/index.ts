import type { Component } from 'vue'
import type { PortalRoute } from '../routes'
import UnavailablePage from './UnavailablePage.vue'
import OverviewPage from './OverviewPage.vue'
import AdminSystemPage from './AdminSystemPage.vue'

/** Pages backed by a browser API. Every other route renders the honest unavailable page. */
const pages: Record<string, Component> = {
  overview: OverviewPage,
  'admin-system': AdminSystemPage,
}

export function pageFor(route: PortalRoute): Component {
  return pages[route.name] || UnavailablePage
}

export function implementedPageNames(): string[] {
  return Object.keys(pages)
}
