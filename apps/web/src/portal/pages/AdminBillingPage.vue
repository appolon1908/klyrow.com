<script setup lang="ts">
import { computed, ref } from 'vue'
import { appApi, type BrowserSession } from '../../api'
import type { PortalRoute } from '../routes'
import type { AdminBillingOverview, AdminBillingSubscription } from '../types'
import { normalizeFailure } from '../errors'
import { notify } from '../toasts'
import { settle } from '../composables/usePage'
import type { PortalFailure } from '../errors'
import PageHeader from '../components/PageHeader.vue'
import MetricCard from '../components/MetricCard.vue'
import PanelCard from '../components/PanelCard.vue'
import DataTable from '../components/DataTable.vue'
import StatusBadge from '../components/StatusBadge.vue'
import LoadingState from '../components/LoadingState.vue'
import ErrorState from '../components/ErrorState.vue'
import ForbiddenState from '../components/ForbiddenState.vue'
import DegradedState from '../components/DegradedState.vue'

defineProps<{ route: PortalRoute; params: Record<string, string>; session: BrowserSession }>()

const loading = ref(true)
const dunning = ref(false)
const overview = ref<AdminBillingOverview | null>(null)
const subscriptions = ref<AdminBillingSubscription[]>([])
const overviewFailure = ref<PortalFailure | null>(null)
const subscriptionsFailure = ref<PortalFailure | null>(null)
const graceDays = ref(7)
const suspendDays = ref(21)

const subscriptionColumns = [
  { key: 'tenant_name', label: 'Tenant' },
  { key: 'plan_name', label: 'Plan' },
  { key: 'status', label: 'Status' },
  { key: 'billing_cycle', label: 'Cycle' },
  { key: 'period_end', label: 'Period end' },
  { key: 'open_invoice_count', label: 'Open invoices' },
]
const invoiceColumns = [
  { key: 'number', label: 'Invoice' },
  { key: 'tenant_name', label: 'Tenant' },
  { key: 'status', label: 'Status' },
  { key: 'total', label: 'Total' },
  { key: 'due_at', label: 'Due' },
]
const priceColumns = [
  { key: 'plan_name', label: 'Plan' },
  { key: 'version', label: 'Version' },
  { key: 'billing_cycle', label: 'Cycle' },
  { key: 'base_amount', label: 'Base' },
  { key: 'included_units', label: 'Included units' },
  { key: 'effective_at', label: 'Effective' },
]

function count(values: Record<string, number> | undefined) {
  return Object.values(values || {}).reduce((total, value) => total + Number(value || 0), 0)
}
const subscriptionCount = computed(() => count(overview.value?.counts.subscriptions))
const invoiceCount = computed(() => count(overview.value?.counts.invoices))
const paymentCount = computed(() => count(overview.value?.counts.payments))
const refundCount = computed(() => count(overview.value?.counts.refunds))

function date(value: unknown) {
  if (!value) return '—'
  const parsed = new Date(String(value))
  return Number.isNaN(parsed.getTime()) ? '—' : parsed.toLocaleString()
}
function amount(value: unknown, currency?: unknown) {
  const n = Number(value)
  return Number.isFinite(n) ? `${currency || ''} ${n.toFixed(2)}`.trim() : '—'
}

async function load() {
  loading.value = true
  const result = await settle({
    overview: () => appApi<AdminBillingOverview>('/app/api/admin/billing/overview'),
    subscriptions: () => appApi<AdminBillingSubscription[]>('/app/api/admin/billing/subscriptions'),
  })
  overview.value = result.overview.value
  subscriptions.value = result.subscriptions.value || []
  overviewFailure.value = result.overview.failure
  subscriptionsFailure.value = result.subscriptions.failure
  loading.value = false
}

async function runDunning() {
  dunning.value = true
  try {
    const result = await appApi<{ processed: number }>('/app/api/admin/billing/dunning', {
      method: 'POST',
      body: JSON.stringify({ grace_days: Number(graceDays.value), suspend_days: Number(suspendDays.value) }),
    })
    notify('success', `Dunning reviewed ${result.processed} overdue invoice(s).`)
    await load()
  } catch (error) {
    const failure = normalizeFailure(error)
    notify('error', failure.code, failure.requestId)
  } finally {
    dunning.value = false
  }
}

load()
</script>

<template>
  <div>
    <PageHeader title="Platform billing" eyebrow="Platform operations" description="Read Klyrow's canonical billing state across tenants. Provider secrets are never exposed here and live charging cannot be enabled from the admin portal.">
      <button type="button" class="kp-button" :disabled="loading" @click="load">Refresh</button>
    </PageHeader>

    <LoadingState v-if="loading" label="Loading platform billing…" />
    <ForbiddenState v-else-if="overviewFailure?.kind === 'forbidden' || subscriptionsFailure?.kind === 'forbidden'" reason="platform-admin" :request-id="overviewFailure?.requestId || subscriptionsFailure?.requestId" :code="overviewFailure?.code || subscriptionsFailure?.code" />
    <ErrorState v-else-if="overviewFailure && subscriptionsFailure" :failure="overviewFailure" @retry="load" />

    <div v-else class="kp-stack">
      <section v-if="overview" class="kp-grid" aria-label="Platform billing counts">
        <MetricCard label="Subscriptions" :value="subscriptionCount" source="live" />
        <MetricCard label="Invoices" :value="invoiceCount" source="live" />
        <MetricCard label="Payments" :value="paymentCount" source="live" />
        <MetricCard label="Refunds" :value="refundCount" source="live" />
        <MetricCard label="Billing drift" :value="overview.billing_drift.issue_count" :detail="overview.billing_drift.status" source="live" />
      </section>

      <PanelCard title="Billing capability" eyebrow="Fail-closed configuration" :source="overviewFailure ? 'degraded' : 'live'">
        <DegradedState v-if="overviewFailure" message="Billing capability could not be read." :request-id="overviewFailure.requestId" :code="overviewFailure.code" />
        <dl v-else-if="overview" class="kp-definition-list">
          <div><dt>Configuration</dt><dd><StatusBadge :value="overview.configuration.valid ? 'VALID' : 'INVALID'" /></dd></div>
          <div><dt>Billing</dt><dd><StatusBadge :value="overview.configuration.enabled ? 'ENABLED' : 'DISABLED'" /></dd></div>
          <div><dt>Live charging</dt><dd><StatusBadge :value="overview.configuration.live_charging_enabled ? 'ENABLED' : 'DISABLED'" /></dd></div>
          <div><dt>Dunning</dt><dd><StatusBadge :value="overview.configuration.dunning_enabled ? 'ENABLED' : 'DISABLED'" /></dd></div>
          <div><dt>Providers</dt><dd><code class="kp-code">{{ JSON.stringify(overview.configuration.providers) }}</code></dd></div>
        </dl>
      </PanelCard>

      <PanelCard title="Dunning" eyebrow="Bounded operator command">
        <form class="kp-form" aria-label="Run billing dunning" @submit.prevent="runDunning">
          <div class="kp-grid">
            <div class="kp-field"><label for="grace-days">Grace days</label><input id="grace-days" v-model.number="graceDays" type="number" min="1" max="90" /></div>
            <div class="kp-field"><label for="suspend-days">Suspend days</label><input id="suspend-days" v-model.number="suspendDays" type="number" min="2" max="180" /></div>
          </div>
          <p class="kp-notice">Dunning never disables login. It can move overdue subscriptions through past-due, grace-period and suspended sending states only when the billing dunning gate is enabled.</p>
          <div class="kp-form__actions"><button type="submit" class="kp-button--primary" :disabled="dunning || !overview?.configuration.dunning_enabled">{{ dunning ? 'Running…' : 'Run dunning' }}</button></div>
        </form>
      </PanelCard>

      <PanelCard title="Subscriptions" eyebrow="All tenants" :source="subscriptionsFailure ? 'degraded' : 'live'">
        <DegradedState v-if="subscriptionsFailure" message="Subscriptions could not be read." :request-id="subscriptionsFailure.requestId" :code="subscriptionsFailure.code" />
        <DataTable v-else caption="Platform subscriptions" :columns="subscriptionColumns" :rows="subscriptions" empty-message="No subscriptions.">
          <template #cell-tenant_name="{ row }">{{ row.tenant_name || row.tenant_id }}</template>
          <template #cell-status="{ value }"><StatusBadge :value="String(value)" /></template>
          <template #cell-period_end="{ value }">{{ date(value) }}</template>
        </DataTable>
      </PanelCard>

      <PanelCard v-if="overview" title="Recent invoices" eyebrow="Canonical ledger authority">
        <DataTable caption="Recent platform invoices" :columns="invoiceColumns" :rows="overview.recent_invoices" empty-message="No invoices.">
          <template #cell-tenant_name="{ row }">{{ row.tenant_name || row.tenant_id }}</template>
          <template #cell-status="{ value }"><StatusBadge :value="String(value)" /></template>
          <template #cell-total="{ row }">{{ amount(row.total, row.currency) }}</template>
          <template #cell-due_at="{ value }">{{ date(value) }}</template>
        </DataTable>
      </PanelCard>

      <PanelCard v-if="overview" title="Active price versions" eyebrow="Catalog">
        <DataTable caption="Active billing price versions" :columns="priceColumns" :rows="overview.active_prices" row-key="price_id" empty-message="No active prices.">
          <template #cell-base_amount="{ row }">{{ amount(row.base_amount, row.currency) }}</template>
          <template #cell-effective_at="{ value }">{{ date(value) }}</template>
        </DataTable>
      </PanelCard>
    </div>
  </div>
</template>
