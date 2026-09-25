<script setup lang="ts">
import { computed } from 'vue'
import { appApi, type BrowserSession } from '../../api'
import type { PortalRoute } from '../routes'
import type { BillingCatalog, BillingProviderCapabilities, BillingSubscription } from '../types'
import { settle, usePage } from '../composables/usePage'
import PageHeader from '../components/PageHeader.vue'
import PanelCard from '../components/PanelCard.vue'
import DataTable from '../components/DataTable.vue'
import StatusBadge from '../components/StatusBadge.vue'
import LoadingState from '../components/LoadingState.vue'
import ErrorState from '../components/ErrorState.vue'
import ForbiddenState from '../components/ForbiddenState.vue'

defineProps<{ route: PortalRoute; params: Record<string, string>; session: BrowserSession }>()

const page = usePage(async () => {
  const sources = await settle({
    catalog: () => appApi<BillingCatalog>('/app/api/billing/catalog'),
    subscription: () => appApi<BillingSubscription>('/app/api/billing/subscription'),
    capabilities: () => appApi<BillingProviderCapabilities>('/app/api/billing/capabilities'),
  })
  if (!sources.catalog.value && !sources.subscription.value && !sources.capabilities.value) {
    throw new Error('billing_plan_unavailable')
  }
  return sources
})

const catalog = computed(() => page.data.value?.catalog.value?.items || [])
const subscription = computed(() => page.data.value?.subscription.value || null)
const capabilities = computed(() => page.data.value?.capabilities.value || null)
const degraded = computed(() => Boolean(page.data.value && (
  page.data.value.catalog.failure || page.data.value.subscription.failure || page.data.value.capabilities.failure
)))
const currentPlan = computed(() => {
  if (!subscription.value) return null
  return catalog.value.find(item => item.name === subscription.value?.plan || item.code === subscription.value?.plan) || null
})
const planRows = computed(() => catalog.value.map(item => ({
  id: item.code,
  name: item.name,
  price: money(item.base_amount, item.currency),
  cycle: readable(item.billing_cycle),
  included: item.included_units.toLocaleString(),
  overage: money(item.overage_amount, item.currency),
})))
const planColumns = [
  { key: 'name', label: 'Plan' },
  { key: 'price', label: 'Base price' },
  { key: 'cycle', label: 'Billing cycle' },
  { key: 'included', label: 'Included units' },
  { key: 'overage', label: 'Overage' },
]

function money(value: number | string | null | undefined, currency = 'USD') {
  if (value === null || value === undefined) return 'Not available'
  return new Intl.NumberFormat(undefined, { style: 'currency', currency }).format(Number(value))
}
function date(value?: string | null) {
  return value ? new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' }).format(new Date(value)) : 'Not available'
}
function readable(value?: string | null) {
  return value ? value.replaceAll('_', ' ') : 'Not available'
}
function featureLabel(key: string) {
  return key.replaceAll('_', ' ')
}
function featureValue(value: unknown) {
  if (typeof value === 'boolean') return value ? 'Included' : 'Not included'
  if (typeof value === 'number') return value.toLocaleString()
  if (typeof value === 'string') return value
  return JSON.stringify(value)
}
</script>

<template>
  <div>
    <PageHeader title="Plan" eyebrow="Billing" description="Plan, price and entitlement information from the authenticated billing service. This page does not perform provider settlement.">
      <button type="button" class="kp-button" :disabled="page.status.value === 'loading'" @click="page.reload">Refresh</button>
    </PageHeader>
    <LoadingState v-if="page.status.value === 'loading'" label="Loading plan…" />
    <ForbiddenState v-else-if="page.status.value === 'forbidden'" reason="server" :request-id="page.failure.value?.requestId" :code="page.failure.value?.code" />
    <ErrorState v-else-if="page.failure.value" :failure="page.failure.value" @retry="page.reload" />
    <div v-else class="kp-stack">
      <div v-if="degraded" class="kp-notice kp-notice--warning" role="status">
        Some billing sources are temporarily unavailable. The information below is the authoritative data that could be read successfully.
      </div>
      <div class="kp-notice" role="status">
        <strong>Provider capability:</strong>
        <template v-if="capabilities">
          {{ capabilities.billing_enabled ? 'Billing enabled.' : 'Billing disabled.' }}
          {{ capabilities.live_charging ? ' Live charging is enabled by the billing gate.' : ' Live charging is not enabled.' }}
        </template>
        <template v-else>Capability status is temporarily unavailable.</template>
      </div>

      <PanelCard title="Current subscription" eyebrow="Authoritative billing state">
        <dl v-if="subscription" class="kp-definition-list">
          <div><dt>Product and plan</dt><dd>{{ subscription.product }} · {{ subscription.plan }}</dd></div>
          <div><dt>Status</dt><dd><StatusBadge :value="subscription.status" /></dd></div>
          <div><dt>Billing interval</dt><dd>{{ readable(subscription.interval) }}</dd></div>
          <div><dt>Price</dt><dd>{{ money(subscription.price, subscription.currency || undefined) }}</dd></div>
          <div><dt>Renews</dt><dd>{{ date(subscription.renews_at) }}</dd></div>
          <div><dt>Trial ends</dt><dd>{{ date(subscription.trial_end) }}</dd></div>
        </dl>
        <p v-else>No active subscription is reported for this organization.</p>
      </PanelCard>

      <PanelCard v-if="currentPlan && Object.keys(currentPlan.features || {}).length" title="Included capabilities" eyebrow="Current plan">
        <dl class="kp-definition-list">
          <div v-for="[key, value] in Object.entries(currentPlan.features)" :key="key">
            <dt>{{ featureLabel(key) }}</dt><dd>{{ featureValue(value) }}</dd>
          </div>
        </dl>
      </PanelCard>

      <PanelCard title="Available plans" eyebrow="Current catalog">
        <DataTable caption="Available billing plans" :columns="planColumns" :rows="planRows" empty-message="No active priced plans are available." />
        <p class="kp-muted">Plan changes use the separately governed subscription lifecycle. This read-only page does not bypass proration, version checks, checkout, or provider gates.</p>
      </PanelCard>
    </div>
  </div>
</template>
