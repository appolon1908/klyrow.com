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
import AnalyticsOverviewPage from './AnalyticsOverviewPage.vue'
import DeliverabilityPage from './DeliverabilityPage.vue'
import DeveloperLogsPage from './DeveloperLogsPage.vue'
import BillingPlanPage from './BillingPlanPage.vue'
import BillingUsagePage from './BillingUsagePage.vue'
import BillingPortalPage from './BillingPortalPage.vue'
import SettingsOrganizationPage from './SettingsOrganizationPage.vue'
import SettingsTeamPage from './SettingsTeamPage.vue'
import SettingsSecurityPage from './SettingsSecurityPage.vue'
import SettingsEnterpriseIdentityPage from './SettingsEnterpriseIdentityPage.vue'
import AdminCountsPage from './AdminCountsPage.vue'
import MediaLibraryPage from './MediaLibraryPage.vue'
import AudienceProfilesPage from './AudienceProfilesPage.vue'
import AudienceProfilePage from './AudienceProfilePage.vue'
import EmailSuppressionsPage from './EmailSuppressionsPage.vue'

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
  'email-suppressions': EmailSuppressionsPage,
  'audience-profiles': AudienceProfilesPage,
  'audience-profile': AudienceProfilePage,
  'content-media': MediaLibraryPage,
  'analytics-overview': AnalyticsOverviewPage,
  deliverability: DeliverabilityPage,
  'deliverability-domain': EmailDomainPage,
  'developer-logs': DeveloperLogsPage,
  'billing-plan': BillingPlanPage,
  'billing-usage': BillingUsagePage,
  'billing-overview': BillingPortalPage,
  'billing-subscription': BillingPortalPage,
  'billing-invoices': BillingPortalPage,
  'billing-invoice': BillingPortalPage,
  'billing-payments': BillingPortalPage,
  'billing-refunds': BillingPortalPage,
  'billing-payment-methods': BillingPortalPage,
  'billing-wallet': BillingPortalPage,
  'settings-organization': SettingsOrganizationPage,
  'settings-team': SettingsTeamPage,
  'settings-security': SettingsSecurityPage,
  'settings-sso': SettingsEnterpriseIdentityPage,
  'settings-scim': SettingsEnterpriseIdentityPage,
  'admin-tenants': AdminCountsPage,
  'admin-queues': AdminCountsPage,
  'admin-deliverability': AdminCountsPage,
}

export function pageFor(route: PortalRoute): Component {
  return pages[route.name] || UnavailablePage
}

export function implementedPageNames(): string[] {
  return Object.keys(pages)
}
