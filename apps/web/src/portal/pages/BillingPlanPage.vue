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

defineProps<{ route: PortalRoute; params: Record<string, string>; session: BrowserSession }>()
const page = usePage(() => appApi<DashboardData>('/app/api/dashboard'), { cacheKey: 'dashboard' })
const metrics = computed(() => page.data.value?.metrics || null)
const source = computed(() => (page.stale.value ? 'stale' : 'live'))
</script>

<template>
  <div>
    <PageHeader title="Plan" eyebrow="Billing" description="Provider-agnostic billing information. Payment behaviour in this release is manual or sandbox only; no live charging can be initiated from the portal.">
      <button type="button" class="kp-button" :disabled="page.status.value === 'loading'" @click="page.reload">Refresh</button>
    </PageHeader>
    <LoadingState v-if="page.status.value === 'loading'" label="Loading plan…" />
    <ForbiddenState v-else-if="page.status.value === 'forbidden'" reason="server" :request-id="page.failure.value?.requestId" :code="page.failure.value?.code" />
    <ErrorState v-else-if="page.failure.value" :failure="page.failure.value" @retry="page.reload" />
    <div v-else class="kp-stack">
      <section class="kp-grid" aria-label="Plan limits">
        <MetricCard label="Daily message quota" :value="metrics ? metrics.quota : null" detail="Enforced by the sending policy engine" :source="source" />
        <MetricCard label="Sent · last 24h" :value="metrics ? metrics.sent_24h : null" :source="source" />
        <MetricCard label="Subscription" :value="null" detail="Needs the subscription contract" source="unavailable" />
      </section>
      <div class="kp-notice kp-notice--warning" role="status"><strong>Payment behaviour:</strong> manual or sandbox only. Live payment actions are disabled and no payment provider is active.</div>
      <PanelCard title="Subscription and plan detail" eyebrow="Available" source="live">
        <p>Open <a href="/app/billing/subscription">Subscription</a> for the current plan, interval, price and renewal status.</p>
      </PanelCard>
    </div>
  </div>
</template>
