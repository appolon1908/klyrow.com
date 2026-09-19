<script setup lang="ts">
import { computed } from 'vue'
import type { PortalRoute } from '../routes'
import PageHeader from '../components/PageHeader.vue'
import UnavailableState from '../components/UnavailableState.vue'

const props = defineProps<{ route: PortalRoute; params: Record<string, string> }>()
const ALTERNATIVES: Record<string, Array<{ label: string; href: string }>> = {
  Email: [{ label: 'Messages', href: '/app/email/messages' }, { label: 'Domains', href: '/app/email/domains' }],
  Content: [{ label: 'Send a transactional email', href: '/app' }],
  Audience: [{ label: 'Suppression counts on overview', href: '/app/overview' }],
  Campaigns: [{ label: 'Overview', href: '/app/overview' }],
  Journeys: [{ label: 'Overview', href: '/app/overview' }],
  Analytics: [{ label: 'Analytics overview', href: '/app/analytics/overview' }],
  Deliverability: [{ label: 'Domain deliverability', href: '/app/deliverability' }],
  Developer: [{ label: 'Message logs', href: '/app/developer/logs' }],
  Billing: [{ label: 'Plan', href: '/app/billing/plan' }, { label: 'Usage', href: '/app/billing/usage' }],
  Settings: [{ label: 'Organization', href: '/app/settings/organization' }, { label: 'Security', href: '/app/settings/security' }],
  Support: [{ label: 'Overview', href: '/app/overview' }],
  Admin: [{ label: 'System', href: '/admin/system' }, { label: 'Provisioning operations', href: '/admin/provisioning' }],
}
// What the page will offer once its contract exists, and what is explicitly not claimed today.
const DISCLOSURES: Record<string, string[]> = {
  'email-streams': ['Will list message streams with kind, rate limit, retention and suppression policy.', 'No stream state is inferred from message rows.'],
  'email-suppressions': ['Will list suppressions with reason and source, and allow tenant-scoped add/remove with confirmation.', 'The overview shows only the suppression count from the dashboard API.'],
  'content-templates': ['Will list templates with locale, latest version and publish state.', 'Rendering, publishing and rollback require the template version contract.'],
  'content-template': ['Will show version history, required variables and preview.'],
  'content-builder': ['Will edit subject, plain text and HTML source with a sanitized preview at desktop and mobile widths.', 'No autosave exists; nothing is saved until the contract exists.'],
  'content-media': ['No media library API exists in any audience; uploads are not accepted.'],
  'content-brand': ['No brand settings API exists in any audience.'],
  'audience-profiles': ['Will list profiles with identity fields, attributes, consent and preferences.', 'Contact counts on the overview come from the dashboard API only.'],
  'audience-profile': ['Will show a per-profile timeline once the profile contract exists.'],
  'audience-imports': ['Will create import jobs from an object reference and show proven progress and failure diagnostics.', 'No progress is displayed without a job status API.'],
  'audience-segments': ['Will create and preview segments with the current rule contract.', 'Scalable membership materialization and rebuild are not implemented and are not claimed.'],
  'audience-segment': ['Membership rebuild is not implemented server-side.'],
  'audience-preferences': ['Will distinguish consent (proof, source, version), preference (subscription topics) and suppression (delivery block) records.', 'The distinction between consent, preference and suppression is enforced by the backend contract, not inferred here.'],
  'campaigns': ['Will list campaign definitions with state; sender, template, segment, time zone, tracking and frequency cap come from the campaign contract.'],
  'campaign-new': ['Will provide draft creation, test, preflight and schedule with confirmation for irreversible actions.'],
  'campaign': ['Progress, audience counts and delivery statistics are only shown once the progress/events contract exists; none are fabricated.'],
  'journeys': ['Will list journeys with version and publish state.', 'The durable journey engine is incomplete: waits, branches, webhooks, crash recovery and retries are not supported until the runtime and its tests prove them.'],
  'journey-new': ['The durable journey engine is incomplete; creation is disabled until the contract exists.'],
  'journey-builder': ['The durable journey engine is incomplete: waits, branches, webhooks, crash recovery and retries are not supported until the runtime and its tests prove them.', 'Validation, publish, pause, resume and rollback need the journey graph contract.'],
  'journey-runs': ['Run history is unavailable; the durable journey engine is incomplete.'],
  'analytics-campaigns': ['No revenue or engagement values are invented.'],
  'analytics-journeys': ['No values are invented.'],
  'analytics-segments': ['No values are invented.'],
  'analytics-links': ['No values are invented.'],
  'deliverability-ip-pools': ['IP pool and warmup state will be shown only from the provider-agnostic contract.'],
  'deliverability-alerts': ['Alerts and degradations will come from the alert feed; none are synthesized.'],
  'developer-api-keys': ['Will list, create, rotate and revoke API keys with explicit confirmation; the key value is shown once and never stored in the browser.', 'API keys are available today through the authenticated /v1 API for server integrations.'],
  'developer-service-accounts': ['Will list, create, rotate and revoke service accounts with one-time secret display.'],
  'developer-smtp': ['Will list, create, rotate and revoke SMTP credentials; passwords are shown once and never redisplayed.'],
  'developer-webhooks': ['Will list, create, rotate and test webhook subscriptions; signing secrets are shown once.', 'Delivery history is unavailable until its API exists.'],
  'developer-openapi': ['The OpenAPI document is published from the repository schemas; a same-origin endpoint is required for in-portal viewing and download.'],
  'billing-invoices': ['Invoices will be read-only, provider-agnostic records; no live payment action will be offered.'],
  'billing-invoice': ['Invoice detail will be read-only.'],
  'billing-payment-methods': ['Payment methods will list opaque provider references only; live payment provider actions are disabled by design and no provider branding implies an active integration.', 'Manual, sandbox or unavailable payment behaviour is labelled explicitly.'],
  'settings-sso': ['Sign-in is brokered by Keycloak; tenant-specific SSO configuration is not implemented server-side.'],
  'settings-scim': ['SCIM provisioning is not implemented server-side; no endpoint exists to configure.'],
  'settings-retention': ['Retention policy will be read and updated through its browser contract only.'],
  'settings-audit': ['Audit records will be read-only and tenant-scoped.'],
  'settings-integrations': ['Integrations will be listed and created through their browser contract; no credential is ever displayed twice.'],
  'support': ['Support tickets will be listed and created through their browser contract.'],
  'support-ticket': ['Ticket detail will be read from its browser contract.'],
  'admin-abuse': ['Abuse review will use current admin APIs only; nothing is fabricated.'],
  'admin-reconciliation': ['Reconciliation state will come from its admin contract; no infrastructure status is fabricated.'],
  'admin-billing': ['No payment provider is active; platform billing views will be read-only.'],
  'admin-audit': ['Platform audit will be read-only.'],
}
const alternatives = computed(() => (ALTERNATIVES[props.route.group] || []).filter(item => item.href !== props.route.pattern))
const disclosures = computed(() => DISCLOSURES[props.route.name] || [])
</script>

<template>
  <div>
    <PageHeader :title="route.title" :eyebrow="route.group" />
    <UnavailableState :title="route.title" :dependency="route.dependency" :alternatives="alternatives">
      <ul v-if="disclosures.length" class="kp-list">
        <li v-for="(item, index) in disclosures" :key="index">{{ item }}</li>
      </ul>
      <p v-if="params.id" class="kp-muted">Requested record: <code class="kp-code">{{ params.id }}</code></p>
    </UnavailableState>
  </div>
</template>
