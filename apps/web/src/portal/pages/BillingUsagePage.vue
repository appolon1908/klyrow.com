<script setup lang="ts">
import { computed, ref } from 'vue'
import { appApi, type BrowserSession } from '../../api'
import type { PortalRoute } from '../routes'
import type { BillingEntitlements, BillingUsageHistory } from '../types'
import { settle, usePage } from '../composables/usePage'
import PageHeader from '../components/PageHeader.vue'
import PanelCard from '../components/PanelCard.vue'
import MetricCard from '../components/MetricCard.vue'
import DataTable from '../components/DataTable.vue'
import LoadingState from '../components/LoadingState.vue'
import ErrorState from '../components/ErrorState.vue'
import ForbiddenState from '../components/ForbiddenState.vue'

defineProps<{ route: PortalRoute; params: Record<string, string>; session: BrowserSession }>()

type Granularity = 'daily' | 'monthly'
const granularity = ref<Granularity>('daily')
const cursor = ref('')
const cursorStack = ref<string[]>([])

const page = usePage(async () => {
  const query = new URLSearchParams({ limit: granularity.value === 'daily' ? '31' : '12' })
  if (cursor.value) query.set('cursor', cursor.value)
  const sources = await settle({
    history: () => appApi<BillingUsageHistory>(`/app/api/billing/usage/${granularity.value}?${query}`),
    entitlements: () => appApi<BillingEntitlements>('/app/api/billing/entitlements'),
  })
  if (!sources.history.value && !sources.entitlements.value) throw new Error('billing_usage_unavailable')
  return sources
})

const history = computed(() => page.data.value?.history.value || null)
const entitlements = computed(() => page.data.value?.entitlements.value || null)
const degraded = computed(() => Boolean(page.data.value && (page.data.value.history.failure || page.data.value.entitlements.failure)))
const rows = computed(() => (history.value?.items || []).map(item => ({
  id: item.period_start,
  period: formatPeriod(item.period_start),
  quantity: item.quantity.toLocaleString(),
})))
const total = computed(() => (history.value?.items || []).reduce((sum, item) => sum + item.quantity, 0))
const messageEntitlement = computed(() => {
  const data = entitlements.value?.entitlements || {}
  const candidate = data.messages ?? data.accepted_message
  return candidate && typeof candidate === 'object' ? candidate as { limit?: number; used?: number; remaining?: number } : null
})
const columns = [{ key: 'period', label: 'Period' }, { key: 'quantity', label: 'Metered units' }]

function formatPeriod(value: string) {
  const parsed = new Date(`${value}T00:00:00Z`)
  return new Intl.DateTimeFormat(undefined, granularity.value === 'monthly'
    ? { month: 'long', year: 'numeric', timeZone: 'UTC' }
    : { dateStyle: 'medium', timeZone: 'UTC' }).format(parsed)
}
async function selectGranularity(value: Granularity) {
  granularity.value = value
  cursor.value = ''
  cursorStack.value = []
  await page.reload()
}
async function nextPage() {
  if (!history.value?.next_cursor) return
  cursorStack.value.push(cursor.value)
  cursor.value = history.value.next_cursor
  await page.reload()
}
async function previousPage() {
  if (!cursorStack.value.length) return
  cursor.value = cursorStack.value.pop() || ''
  await page.reload()
}
</script>

<template>
  <div>
    <PageHeader title="Usage" eyebrow="Billing" description="Bounded UTC usage history from the authoritative tenant billing ledger, with entitlement context from the current subscription.">
      <button type="button" class="kp-button" :disabled="page.status.value === 'loading'" @click="page.reload">Refresh</button>
    </PageHeader>
    <LoadingState v-if="page.status.value === 'loading'" label="Loading usage…" />
    <ForbiddenState v-else-if="page.status.value === 'forbidden'" reason="server" :request-id="page.failure.value?.requestId" :code="page.failure.value?.code" />
    <ErrorState v-else-if="page.failure.value" :failure="page.failure.value" @retry="page.reload" />
    <div v-else class="kp-stack">
      <div v-if="degraded" class="kp-notice kp-notice--warning" role="status">
        Usage is degraded because one billing source is unavailable. No estimate or provider-derived value is substituted.
      </div>
      <section class="kp-grid" aria-label="Usage summary">
        <MetricCard label="Loaded period total" :value="total" detail="Authoritative metered units in the loaded ledger page" source="live" />
        <MetricCard label="Entitlement status" :value="entitlements?.status || 'Not available'" :detail="entitlements ? `Version ${entitlements.version}` : 'Subscription entitlement source unavailable'" source="live" />
        <MetricCard label="Remaining message allowance" :value="messageEntitlement?.remaining ?? null" :detail="messageEntitlement?.limit == null ? 'No numeric message limit is reported' : `Limit ${messageEntitlement.limit.toLocaleString()}`" source="live" />
      </section>

      <PanelCard title="Usage history" eyebrow="Authoritative ledger">
        <div class="kp-inline-actions" role="group" aria-label="Usage aggregation">
          <button type="button" class="kp-button" :aria-pressed="granularity === 'daily'" @click="selectGranularity('daily')">Daily</button>
          <button type="button" class="kp-button" :aria-pressed="granularity === 'monthly'" @click="selectGranularity('monthly')">Monthly</button>
        </div>
        <DataTable caption="Billing usage history" :columns="columns" :rows="rows" empty-message="No metered usage exists in this window." />
        <div class="kp-inline-actions" aria-label="Usage pagination">
          <button type="button" class="kp-button" :disabled="!cursorStack.length" @click="previousPage">Previous</button>
          <button type="button" class="kp-button" :disabled="!history?.next_cursor" @click="nextPage">Next</button>
        </div>
        <p v-if="history" class="kp-muted">
          Window: {{ new Date(history.window_start).toLocaleDateString() }} – {{ new Date(history.window_end).toLocaleDateString() }} · Unit: {{ history.unit }}.
          Periods without ledger entries are omitted.
        </p>
      </PanelCard>
    </div>
  </div>
</template>
