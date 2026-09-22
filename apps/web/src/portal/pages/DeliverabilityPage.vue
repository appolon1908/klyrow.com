<script setup lang="ts">
import { computed } from 'vue'
import { appApi, type BrowserSession } from '../../api'
import type { PortalRoute } from '../routes'
import type { DeliverabilityResponse } from '../types'
import { usePage } from '../composables/usePage'
import PageHeader from '../components/PageHeader.vue'
import DataTable from '../components/DataTable.vue'
import StatusBadge from '../components/StatusBadge.vue'
import PanelCard from '../components/PanelCard.vue'
import LoadingState from '../components/LoadingState.vue'
import EmptyState from '../components/EmptyState.vue'
import ErrorState from '../components/ErrorState.vue'
import ForbiddenState from '../components/ForbiddenState.vue'
import MetricCard from '../components/MetricCard.vue'

defineProps<{ route: PortalRoute; params: Record<string, string>; session: BrowserSession }>()
const page = usePage(
  () => appApi<DeliverabilityResponse>('/app/api/deliverability?limit=100&offset=0'),
  { isEmpty: result => result.items.length === 0 },
)
const rows = computed(() => page.data.value?.items || [])
const verified = computed(() => rows.value.filter(item => ['VERIFIED', 'SENDING_ENABLED'].includes(item.state)).length)
const checked = computed(() => rows.value.filter(item => item.source === 'durable_snapshot').length)
const stale = computed(() => rows.value.filter(item => item.stale).length)
const alertCount = computed(() => rows.value.reduce((sum, item) => sum + item.alert_count, 0))
const source = computed(() => rows.value.some(item => item.stale) ? 'stale' : 'live')
const columns = [
  { key: 'domain', label: 'Domain' },
  { key: 'state', label: 'Claim' },
  { key: 'spf', label: 'SPF' },
  { key: 'dkim', label: 'DKIM' },
  { key: 'dmarc', label: 'DMARC' },
  { key: 'ptr', label: 'PTR' },
  { key: 'tls', label: 'TLS' },
  { key: 'alert_count', label: 'Alerts' },
  { key: 'checked_at', label: 'Checked' },
]
</script>

<template>
  <div>
    <PageHeader title="Deliverability" eyebrow="Sending health" description="Tenant-scoped DNS and transport evidence from durable checks. Unknown and stale evidence is shown honestly instead of being treated as healthy.">
      <button type="button" class="kp-button" :disabled="page.status.value === 'loading'" @click="page.reload">Refresh</button>
    </PageHeader>
    <LoadingState v-if="page.status.value === 'loading'" label="Loading deliverability evidence…" />
    <ForbiddenState v-else-if="page.status.value === 'forbidden'" reason="server" :request-id="page.failure.value?.requestId" :code="page.failure.value?.code" />
    <ErrorState v-else-if="page.failure.value" :failure="page.failure.value" @retry="page.reload" />
    <EmptyState v-else-if="page.status.value === 'empty'" title="No domains to assess" description="Claim and verify a sending domain first.">
      <a class="kp-button--primary" href="/app/email/domains">Domains</a>
    </EmptyState>
    <div v-else class="kp-stack">
      <div v-if="stale" class="kp-notice kp-notice--warning" role="status">{{ stale }} domain{{ stale === 1 ? '' : 's' }} have stale or missing evidence. Open a domain to run a fresh check.</div>
      <section class="kp-grid" aria-label="Deliverability counts">
        <MetricCard label="Verified domains" :value="verified" :detail="`of ${rows.length} claimed`" :source="source" />
        <MetricCard label="Checked domains" :value="checked" :detail="`of ${rows.length} claimed`" :source="source" />
        <MetricCard label="Active alerts" :value="alertCount" detail="Across current durable snapshots" :source="source" />
      </section>
      <DataTable caption="Domain deliverability" :columns="columns" :rows="rows">
        <template #cell-domain="{ row }"><a :href="`/app/deliverability/domains/${encodeURIComponent(String(row.id))}`">{{ row.domain }}</a></template>
        <template #cell-state="{ value }"><StatusBadge :value="String(value)" /></template>
        <template #cell-spf="{ value }"><StatusBadge :value="value === null ? 'unknown' : Boolean(value)" /></template>
        <template #cell-dkim="{ value }"><StatusBadge :value="value === null ? 'unknown' : Boolean(value)" /></template>
        <template #cell-dmarc="{ value }"><StatusBadge :value="value === null ? 'unknown' : Boolean(value)" /></template>
        <template #cell-ptr="{ value }"><StatusBadge :value="value === null ? 'unknown' : Boolean(value)" /></template>
        <template #cell-tls="{ value }"><StatusBadge :value="value === null ? 'unknown' : Boolean(value)" /></template>
        <template #cell-alert_count="{ value }">{{ Number(value) || 0 }}</template>
        <template #cell-checked_at="{ row, value }"><span :class="{ 'kp-muted': row.stale }">{{ value ? new Date(String(value)).toLocaleString() : 'Not checked' }}{{ row.stale ? ' · stale' : '' }}</span></template>
      </DataTable>
      <PanelCard title="Authority boundary" eyebrow="Governed evidence" :source="source">
        <p>These checks are Klyrow-owned DNS/readiness evidence. Provider mutation is not performed by this read page; provider-side remediation remains behind the governed Middleware adapter path.</p>
      </PanelCard>
    </div>
  </div>
</template>
