<script setup lang="ts">
import { computed } from 'vue'
import { appApi, type BrowserSession } from '../../api'
import type { PortalRoute } from '../routes'
import type { DashboardData } from '../types'
import { usePage } from '../composables/usePage'
import PageHeader from '../components/PageHeader.vue'
import MetricCard from '../components/MetricCard.vue'
import PanelCard from '../components/PanelCard.vue'
import LoadingState from '../components/LoadingState.vue'
import ErrorState from '../components/ErrorState.vue'
import ForbiddenState from '../components/ForbiddenState.vue'
import UnavailableState from '../components/UnavailableState.vue'

defineProps<{ route: PortalRoute; params: Record<string, string>; session: BrowserSession }>()
const page = usePage(() => appApi<DashboardData>('/app/api/dashboard'), { cacheKey: 'dashboard' })
const metrics = computed(() => page.data.value?.metrics || null)
const source = computed(() => (page.stale.value ? 'stale' : 'live'))
const used = computed(() => metrics.value ? Math.min(100, Math.round((metrics.value.sent_24h / Math.max(1, metrics.value.quota)) * 100)) : 0)
</script>

<template>
  <div>
    <PageHeader title="Usage" eyebrow="Billing" description="Current-window usage from the dashboard API. Usage history by day or month has no browser API yet.">
      <button type="button" class="kp-button" :disabled="page.status.value === 'loading'" @click="page.reload">Refresh</button>
    </PageHeader>
    <LoadingState v-if="page.status.value === 'loading'" label="Loading usage…" />
    <ForbiddenState v-else-if="page.status.value === 'forbidden'" reason="server" :request-id="page.failure.value?.requestId" :code="page.failure.value?.code" />
    <ErrorState v-else-if="page.failure.value" :failure="page.failure.value" @retry="page.reload" />
    <div v-else-if="metrics" class="kp-stack">
      <section class="kp-grid" aria-label="Usage counts">
        <MetricCard label="Sent · last 24h" :value="metrics.sent_24h" :detail="`${used}% of the daily quota (${metrics.quota.toLocaleString()})`" :source="source" />
        <MetricCard label="Messages total" :value="metrics.messages_total" :source="source" />
        <MetricCard label="Contacts" :value="metrics.contacts" :source="source" />
      </section>
      <div class="kp-progress" role="progressbar" aria-label="Daily quota used" :aria-valuenow="used" aria-valuemin="0" aria-valuemax="100"><span :style="{ width: `${used}%` }"></span></div>
      <PanelCard title="Usage history" eyebrow="Not available" source="unavailable">
        <UnavailableState title="Usage history" dependency="No browser API exists yet. Required contract: GET /app/api/billing/usage/daily and /monthly (unit, quantity, period) with cursor pagination." />
      </PanelCard>
    </div>
  </div>
</template>
