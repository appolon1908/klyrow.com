<script setup lang="ts">
import { ref } from 'vue'
import { appApi, type BrowserSession } from '../../api'
import type { PortalRoute } from '../routes'
import type { AdminAbuseAlert, AdminAbuseState, AdminSuspension } from '../types'
import { normalizeFailure } from '../errors'
import { notify } from '../toasts'
import { usePage } from '../composables/usePage'
import PageHeader from '../components/PageHeader.vue'
import MetricCard from '../components/MetricCard.vue'
import PanelCard from '../components/PanelCard.vue'
import DataTable from '../components/DataTable.vue'
import StatusBadge from '../components/StatusBadge.vue'
import LoadingState from '../components/LoadingState.vue'
import ErrorState from '../components/ErrorState.vue'
import ForbiddenState from '../components/ForbiddenState.vue'

defineProps<{ route: PortalRoute; params: Record<string, string>; session: BrowserSession }>()

const page = usePage(() => appApi<AdminAbuseState>('/app/api/admin/abuse'), { cacheKey: 'admin-abuse' })
const evaluating = ref(false)
const suspending = ref(false)
const tenantId = ref('')
const suspendTenant = ref('')
const suspendType = ref<'TENANT' | 'DOMAIN' | 'SENDER'>('TENANT')
const suspendResource = ref('')
const suspendReason = ref('')
const bounceRate = ref('0')
const complaintRate = ref('0')
const invalidRate = ref('0')
const volumeRatio = ref('1')

const alertColumns = [
  { key: 'tenant_name', label: 'Tenant' },
  { key: 'severity', label: 'Severity' },
  { key: 'state', label: 'State' },
  { key: 'kind', label: 'Kind' },
  { key: 'created_at', label: 'Created' },
  { key: 'actions', label: 'Actions' },
]
const suspensionColumns = [
  { key: 'tenant_name', label: 'Tenant' },
  { key: 'resource_type', label: 'Type' },
  { key: 'resource_id', label: 'Resource' },
  { key: 'reason', label: 'Reason' },
  { key: 'created_at', label: 'Created' },
  { key: 'actions', label: 'Actions' },
]

function date(value: unknown) {
  if (!value) return '—'
  const parsed = new Date(String(value))
  return Number.isNaN(parsed.getTime()) ? '—' : parsed.toLocaleString()
}

async function evaluate() {
  if (!tenantId.value.trim()) return
  evaluating.value = true
  try {
    const result = await appApi<{ state: string; suspended: boolean }>('/app/api/admin/abuse/evaluate', {
      method: 'POST',
      body: JSON.stringify({
        tenant_id: tenantId.value.trim(),
        bounce_rate: Number(bounceRate.value),
        complaint_rate: Number(complaintRate.value),
        invalid_rate: Number(invalidRate.value),
        volume_ratio: Number(volumeRatio.value),
      }),
    })
    notify(result.suspended ? 'warning' : 'success', result.suspended ? 'Abuse evaluation suspended the tenant.' : 'Abuse evaluation completed.')
    await page.reload()
  } catch (error) {
    const failure = normalizeFailure(error)
    notify('error', failure.code, failure.requestId)
  } finally {
    evaluating.value = false
  }
}

async function suspend() {
  if (!suspendTenant.value.trim() || !suspendReason.value.trim()) return
  suspending.value = true
  try {
    await appApi('/app/api/admin/abuse/suspensions', {
      method: 'POST',
      body: JSON.stringify({
        resource_type: suspendType.value,
        tenant_id: suspendTenant.value.trim(),
        resource_id: suspendType.value === 'TENANT' ? suspendTenant.value.trim() : suspendResource.value.trim(),
        reason: suspendReason.value.trim(),
      }),
    })
    notify('warning', 'Resource suspended.')
    suspendResource.value = ''
    suspendReason.value = ''
    await page.reload()
  } catch (error) {
    const failure = normalizeFailure(error)
    notify('error', failure.code, failure.requestId)
  } finally {
    suspending.value = false
  }
}

async function changeState(alert: AdminAbuseAlert, state: 'ACKNOWLEDGED' | 'RESOLVED') {
  try {
    await appApi('/app/api/admin/abuse/alerts/' + encodeURIComponent(alert.id) + '/state', {
      method: 'POST',
      body: JSON.stringify({ state }),
    })
    notify('success', state === 'RESOLVED' ? 'Abuse alert resolved.' : 'Abuse alert acknowledged.')
    await page.reload()
  } catch (error) {
    const failure = normalizeFailure(error)
    notify('error', failure.code, failure.requestId)
  }
}

async function release(item: AdminSuspension) {
  try {
    await appApi('/app/api/admin/abuse/suspensions/' + encodeURIComponent(item.id) + '/release', { method: 'POST' })
    notify('success', 'Suspension released.')
    await page.reload()
  } catch (error) {
    const failure = normalizeFailure(error)
    notify('error', failure.code, failure.requestId)
  }
}
</script>

<template>
  <div>
    <PageHeader title="Abuse" eyebrow="Platform operations" description="Review delivery reputation alerts and active platform suspensions. Critical evaluations can suspend a tenant immediately and every state change is audited.">
      <button type="button" class="kp-button" :disabled="page.status.value === 'loading'" @click="page.reload">Refresh</button>
    </PageHeader>

    <LoadingState v-if="page.status.value === 'loading'" label="Loading abuse controls…" />
    <ForbiddenState v-else-if="page.status.value === 'forbidden'" reason="platform-admin" :request-id="page.failure.value?.requestId" :code="page.failure.value?.code" />
    <ErrorState v-else-if="page.failure.value" :failure="page.failure.value" @retry="page.reload" />

    <div v-else-if="page.data.value" class="kp-stack">
      <section class="kp-grid" aria-label="Abuse summary">
        <MetricCard label="Open alerts" :value="page.data.value.summary.open_alerts" source="live" />
        <MetricCard label="Critical open" :value="page.data.value.summary.critical_open" source="live" />
        <MetricCard label="Active suspensions" :value="page.data.value.summary.active_suspensions" source="live" />
      </section>

      <PanelCard title="Evaluate reputation signals" eyebrow="Policy">
        <form class="kp-form" aria-label="Evaluate abuse signals" @submit.prevent="evaluate">
          <div class="kp-field"><label for="abuse-tenant">Tenant ID</label><input id="abuse-tenant" v-model="tenantId" required /></div>
          <div class="kp-grid">
            <div class="kp-field"><label for="bounce-rate">Bounce rate</label><input id="bounce-rate" v-model="bounceRate" type="number" min="0" max="1" step="0.001" required /></div>
            <div class="kp-field"><label for="complaint-rate">Complaint rate</label><input id="complaint-rate" v-model="complaintRate" type="number" min="0" max="1" step="0.0001" required /></div>
            <div class="kp-field"><label for="invalid-rate">Invalid rate</label><input id="invalid-rate" v-model="invalidRate" type="number" min="0" max="1" step="0.001" required /></div>
            <div class="kp-field"><label for="volume-ratio">Volume ratio</label><input id="volume-ratio" v-model="volumeRatio" type="number" min="0" step="0.1" required /></div>
          </div>
          <div class="kp-form__actions"><button type="submit" class="kp-button--primary" :disabled="evaluating">{{ evaluating ? 'Evaluating…' : 'Evaluate signals' }}</button></div>
        </form>
      </PanelCard>

      <PanelCard title="Manual suspension" eyebrow="Bounded operator control">
        <form class="kp-form" aria-label="Suspend a resource" @submit.prevent="suspend">
          <div class="kp-grid">
            <div class="kp-field"><label for="suspend-tenant">Tenant ID</label><input id="suspend-tenant" v-model="suspendTenant" required /></div>
            <div class="kp-field"><label for="suspend-type">Resource type</label><select id="suspend-type" v-model="suspendType"><option value="TENANT">Tenant</option><option value="DOMAIN">Domain</option><option value="SENDER">Sender</option></select></div>
            <div v-if="suspendType !== 'TENANT'" class="kp-field"><label for="suspend-resource">Resource ID</label><input id="suspend-resource" v-model="suspendResource" required /></div>
          </div>
          <div class="kp-field"><label for="suspend-reason">Reason</label><textarea id="suspend-reason" v-model="suspendReason" required maxlength="500"></textarea></div>
          <p class="kp-notice">Suspension is explicit and audited. Use release on the active-suspensions table to restore a resource.</p>
          <div class="kp-form__actions"><button type="submit" class="kp-button--danger" :disabled="suspending || (suspendType !== 'TENANT' && !suspendResource.trim())">{{ suspending ? 'Suspending…' : 'Suspend resource' }}</button></div>
        </form>
      </PanelCard>

      <PanelCard title="Abuse alerts" eyebrow="Current findings">
        <DataTable caption="Abuse alerts" :columns="alertColumns" :rows="page.data.value.alerts" empty-message="No abuse alerts.">
          <template #cell-tenant_name="{ row }"><span>{{ row.tenant_name || row.tenant_id }}</span></template>
          <template #cell-severity="{ value }"><StatusBadge :value="String(value)" /></template>
          <template #cell-state="{ value }"><StatusBadge :value="String(value)" /></template>
          <template #cell-created_at="{ value }">{{ date(value) }}</template>
          <template #cell-actions="{ row }">
            <div class="kp-inline-actions">
              <button v-if="row.state === 'OPEN'" type="button" class="kp-button" @click="changeState(row as unknown as AdminAbuseAlert, 'ACKNOWLEDGED')">Acknowledge</button>
              <button v-if="row.state !== 'RESOLVED'" type="button" class="kp-button" @click="changeState(row as unknown as AdminAbuseAlert, 'RESOLVED')">Resolve</button>
            </div>
          </template>
        </DataTable>
      </PanelCard>

      <PanelCard title="Active suspensions" eyebrow="Delivery controls">
        <DataTable caption="Active delivery suspensions" :columns="suspensionColumns" :rows="page.data.value.suspensions" empty-message="No active suspensions.">
          <template #cell-tenant_name="{ row }"><span>{{ row.tenant_name || row.tenant_id }}</span></template>
          <template #cell-resource_type="{ value }"><StatusBadge :value="String(value)" /></template>
          <template #cell-created_at="{ value }">{{ date(value) }}</template>
          <template #cell-actions="{ row }"><button type="button" class="kp-button--danger" @click="release(row as unknown as AdminSuspension)">Release</button></template>
        </DataTable>
      </PanelCard>
    </div>
  </div>
</template>
