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
</script>

<template>
  <div>
    <PageHeader title="Analytics" eyebrow="Current window" description="Counts from the dashboard API for the last 24 hours and the organization total. Values are displayed as returned; no trend, revenue or rate is estimated.">
      <button type="button" class="kp-button" :disabled="page.status.value === 'loading'" @click="page.reload">Refresh</button>
    </PageHeader>
    <LoadingState v-if="page.status.value === 'loading'" label="Loading analytics…" />
    <ForbiddenState v-else-if="page.status.value === 'forbidden'" reason="server" :request-id="page.failure.value?.requestId" :code="page.failure.value?.code" />
    <ErrorState v-else-if="page.failure.value" :failure="page.failure.value" @retry="page.reload" />
    <div v-else-if="metrics" class="kp-stack">
      <section class="kp-grid" aria-label="Delivery counts">
        <MetricCard label="Sent · last 24h" :value="metrics.sent_24h" :source="source" />
        <MetricCard label="Delivered" :value="metrics.delivered" :source="source" />
        <MetricCard label="Bounced" :value="metrics.bounced" :source="source" />
        <MetricCard label="Delivery rate" :value="`${(metrics.delivery_rate * 100).toFixed(1)}%`" detail="As computed by the dashboard API" :source="source" />
        <MetricCard label="Messages total" :value="metrics.messages_total" :source="source" />
        <MetricCard label="Suppressions" :value="metrics.suppressions" :source="source" />
      </section>
      <p class="kp-muted">Data freshness: read at {{ new Date().toLocaleTimeString() }} from the same store that serves the dashboard. Filters by date, domain, stream, campaign or segment need the historical aggregation contract below.</p>
      <PanelCard title="Historical aggregation" eyebrow="Not available" source="unavailable">
        <UnavailableState title="Historical aggregation and drill-down" dependency="No browser API exists yet. Required contract: GET /app/api/analytics/overview?from&to&domain&stream (daily delivered/bounced/complained series) and per-campaign, journey, segment and link drill-downs." />
      </PanelCard>
    </div>
  </div>
</template>
