<script setup lang="ts">
import { computed } from 'vue'
import { appApi, type BrowserSession } from '../../api'
import type { PortalRoute } from '../routes'
import type { DomainClaim } from '../types'
import { usePage } from '../composables/usePage'
import PageHeader from '../components/PageHeader.vue'
import DataTable from '../components/DataTable.vue'
import StatusBadge from '../components/StatusBadge.vue'
import PanelCard from '../components/PanelCard.vue'
import LoadingState from '../components/LoadingState.vue'
import EmptyState from '../components/EmptyState.vue'
import ErrorState from '../components/ErrorState.vue'
import ForbiddenState from '../components/ForbiddenState.vue'
import UnavailableState from '../components/UnavailableState.vue'
import MetricCard from '../components/MetricCard.vue'

defineProps<{ route: PortalRoute; params: Record<string, string>; session: BrowserSession }>()
const page = usePage(() => appApi<DomainClaim[]>('/app/api/domains'), { isEmpty: rows => rows.length === 0, cacheKey: 'domains' })
const source = computed(() => (page.stale.value ? 'stale' : 'live'))
const verified = computed(() => (page.data.value || []).filter(item => ['VERIFIED', 'SENDING_ENABLED'].includes(item.state)).length)
const suspended = computed(() => (page.data.value || []).filter(item => item.suspended_at).length)
const columns = [{ key: 'domain', label: 'Domain' }, { key: 'state', label: 'Claim state' }, { key: 'dkim', label: 'DKIM' }, { key: 'suspended_at', label: 'Suspended' }, { key: 'verified_at', label: 'Verified' }]
const rows = computed(() => (page.data.value || []).map(item => ({ ...item, dkim: `${item.dkim_selector || '—'} v${item.dkim_version ?? 1}` })))
</script>

<template>
  <div>
    <PageHeader title="Deliverability" eyebrow="Sending health" description="Domain claim states and DKIM identifiers from the domain API. DNS, TLS and PTR checks, reputation and trends are shown only once their API exists.">
      <button type="button" class="kp-button" :disabled="page.status.value === 'loading'" @click="page.reload">Refresh</button>
    </PageHeader>
    <LoadingState v-if="page.status.value === 'loading'" label="Loading domain states…" />
    <ForbiddenState v-else-if="page.status.value === 'forbidden'" reason="server" :request-id="page.failure.value?.requestId" :code="page.failure.value?.code" />
    <ErrorState v-else-if="page.failure.value" :failure="page.failure.value" @retry="page.reload" />
    <EmptyState v-else-if="page.status.value === 'empty'" title="No domains to assess" description="Claim and verify a sending domain first.">
      <a class="kp-button--primary" href="/app/email/domains">Domains</a>
    </EmptyState>
    <div v-else class="kp-stack">
      <section class="kp-grid" aria-label="Domain health counts">
        <MetricCard label="Verified domains" :value="verified" :detail="`of ${(page.data.value || []).length} claimed`" :source="source" />
        <MetricCard label="Suspended domains" :value="suspended" :source="source" />
        <MetricCard label="Reputation state" :value="null" detail="Needs the reputation contract" source="unavailable" />
      </section>
      <DataTable caption="Domain deliverability" :columns="columns" :rows="rows">
        <template #cell-domain="{ row }"><a :href="`/app/deliverability/domains/${encodeURIComponent(String(row.id))}`">{{ row.domain }}</a></template>
        <template #cell-state="{ value }"><StatusBadge :value="String(value)" /></template>
        <template #cell-suspended_at="{ value }">{{ value ? new Date(String(value)).toLocaleDateString() : 'No' }}</template>
        <template #cell-verified_at="{ value }">{{ value ? new Date(String(value)).toLocaleDateString() : 'Not verified' }}</template>
      </DataTable>
      <PanelCard title="DNS/TLS/PTR checks, trends and remediation" eyebrow="Not available" source="unavailable">
        <UnavailableState title="DNS/TLS/PTR checks and trends" dependency="No browser API exists yet. Required contract: GET /app/api/deliverability (per-domain SPF/DKIM/DMARC/PTR/TLS results with checked-at, queue/defer/bounce/complaint trends, warmup and reputation state, remediation guidance)." :alternatives="[{ label: 'IP pools', href: '/app/deliverability/ip-pools' }, { label: 'Alerts', href: '/app/deliverability/alerts' }]" />
      </PanelCard>
    </div>
  </div>
</template>
