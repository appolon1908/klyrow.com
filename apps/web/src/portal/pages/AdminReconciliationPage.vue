<script setup lang="ts">
import { computed, ref } from 'vue'
import { appApi, type BrowserSession } from '../../api'
import type { PortalRoute } from '../routes'
import type { AdminBillingReconciliation, AdminReconciliationList, AdminReconciliationRun } from '../types'
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
const running = ref(false)
const runs = ref<AdminReconciliationRun[]>([])
const billing = ref<AdminBillingReconciliation | null>(null)
const runsFailure = ref<PortalFailure | null>(null)
const billingFailure = ref<PortalFailure | null>(null)

const runColumns = [
  { key: 'kind', label: 'Kind' },
  { key: 'state', label: 'State' },
  { key: 'drift_count', label: 'Drift' },
  { key: 'detail_count', label: 'Findings' },
  { key: 'started_at', label: 'Started' },
]
const issueColumns = [
  { key: 'code', label: 'Finding' },
  { key: 'tenant_id', label: 'Tenant' },
  { key: 'resource_id', label: 'Resource' },
]

const platformDrift = computed(() => runs.value.reduce((total, item) => total + Number(item.drift_count || 0), 0))

function date(value: unknown) {
  if (!value) return '—'
  const parsed = new Date(String(value))
  return Number.isNaN(parsed.getTime()) ? '—' : parsed.toLocaleString()
}

async function load() {
  loading.value = true
  const result = await settle({
    runs: () => appApi<AdminReconciliationList>('/app/api/admin/reconciliation'),
    billing: () => appApi<AdminBillingReconciliation>('/app/api/admin/reconciliation/billing'),
  })
  runs.value = result.runs.value?.runs || []
  billing.value = result.billing.value
  runsFailure.value = result.runs.failure
  billingFailure.value = result.billing.failure
  loading.value = false
}

async function runCheck() {
  running.value = true
  try {
    const result = await appApi<{ state: string; drift_count: number }>('/app/api/admin/reconciliation', { method: 'POST' })
    notify(result.state === 'PASS' ? 'success' : 'warning', result.state === 'PASS' ? 'Platform reconciliation passed.' : `Platform reconciliation found ${result.drift_count} drift item(s).`)
    await load()
  } catch (error) {
    const failure = normalizeFailure(error)
    notify('error', failure.code, failure.requestId)
  } finally {
    running.value = false
  }
}

load()
</script>

<template>
  <div>
    <PageHeader title="Reconciliation" eyebrow="Platform operations" description="Read durable reconciliation evidence and run a bounded platform drift check. Reconciliation reports findings only; it never silently rewrites billing or delivery history.">
      <button type="button" class="kp-button" :disabled="loading" @click="load">Refresh</button>
      <button type="button" class="kp-button--primary" :disabled="running" @click="runCheck">{{ running ? 'Running…' : 'Run platform check' }}</button>
    </PageHeader>

    <LoadingState v-if="loading" label="Loading reconciliation evidence…" />
    <ForbiddenState v-else-if="runsFailure?.kind === 'forbidden' || billingFailure?.kind === 'forbidden'" reason="platform-admin" :request-id="runsFailure?.requestId || billingFailure?.requestId" :code="runsFailure?.code || billingFailure?.code" />
    <ErrorState v-else-if="runsFailure && billingFailure" :failure="runsFailure" @retry="load" />

    <div v-else class="kp-stack">
      <section class="kp-grid" aria-label="Reconciliation summary">
        <MetricCard label="Recorded runs" :value="runs.length" :source="runsFailure ? 'degraded' : 'live'" />
        <MetricCard label="Platform drift items" :value="runsFailure ? null : platformDrift" :source="runsFailure ? 'degraded' : 'live'" />
        <MetricCard label="Billing drift items" :value="billingFailure ? null : billing?.issue_count ?? 0" :source="billingFailure ? 'degraded' : 'live'" />
      </section>

      <PanelCard title="Platform reconciliation history" eyebrow="Durable runs" :source="runsFailure ? 'degraded' : 'live'">
        <DegradedState v-if="runsFailure" message="Platform reconciliation history could not be read." :request-id="runsFailure.requestId" :code="runsFailure.code" />
        <DataTable v-else caption="Platform reconciliation history" :columns="runColumns" :rows="runs" empty-message="No platform reconciliation runs recorded.">
          <template #cell-state="{ value }"><StatusBadge :value="String(value)" /></template>
          <template #cell-started_at="{ value }">{{ date(value) }}</template>
        </DataTable>
      </PanelCard>

      <PanelCard title="Billing reconciliation" eyebrow="Ledger and provider-event truth" :source="billingFailure ? 'degraded' : 'live'">
        <DegradedState v-if="billingFailure" message="Billing reconciliation could not be read." :request-id="billingFailure.requestId" :code="billingFailure.code" />
        <template v-else-if="billing">
          <p class="kp-notice" role="status">Status: <strong>{{ billing.status }}</strong>. Auto-correction is {{ billing.auto_corrected ? 'enabled' : 'disabled' }}.</p>
          <DataTable caption="Billing reconciliation findings" :columns="issueColumns" :rows="billing.issues" empty-message="No billing drift detected.">
            <template #cell-code="{ value }"><code class="kp-code">{{ value }}</code></template>
            <template #cell-tenant_id="{ value }"><code class="kp-code">{{ value || '—' }}</code></template>
            <template #cell-resource_id="{ value }"><code class="kp-code">{{ value || '—' }}</code></template>
          </DataTable>
        </template>
      </PanelCard>
    </div>
  </div>
</template>
