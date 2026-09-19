import type { Component } from 'vue'
import type { PortalRoute } from '../routes'
import UnavailablePage from './UnavailablePage.vue'
import OverviewPage from './OverviewPage.vue'
import AdminSystemPage from './AdminSystemPage.vue'
import EmailMessagesPage from './EmailMessagesPage.vue'
import EmailMessagePage from './EmailMessagePage.vue'
import EmailDomainsPage from './EmailDomainsPage.vue'
import EmailDomainPage from './EmailDomainPage.vue'
import EmailSendersPage from './EmailSendersPage.vue'
import EmailInboundPage from './EmailInboundPage.vue'

/** Pages backed by a browser API. Every other route renders the honest unavailable page. */
const pages: Record<string, Component> = {
  overview: OverviewPage,
  'admin-system': AdminSystemPage,
  'email-messages': EmailMessagesPage,
  'email-message': EmailMessagePage,
  'email-domains': EmailDomainsPage,
  'email-domain': EmailDomainPage,
  'email-senders': EmailSendersPage,
  'email-inbound': EmailInboundPage,
}

export function pageFor(route: PortalRoute): Component {
  return pages[route.name] || UnavailablePage
}

export function implementedPageNames(): string[] {
  return Object.keys(pages)
}
