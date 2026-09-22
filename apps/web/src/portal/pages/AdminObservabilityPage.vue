<script setup lang="ts">
import { computed } from 'vue'
import { appApi, type BrowserSession } from '../../api'
import type { PortalRoute } from '../routes'
import { usePage } from '../composables/usePage'
import PageHeader from '../components/PageHeader.vue'
import MetricCard from '../components/MetricCard.vue'
import PanelCard from '../components/PanelCard.vue'
import LoadingState from '../components/LoadingState.vue'
import ErrorState from '../components/ErrorState.vue'
import ForbiddenState from '../components/ForbiddenState.vue'

interface ObservabilitySnapshot {
  generated_at: string
  architecture: string
  inbound: { accepted: number; quarantined: number; rejected: number }
  outbound: { active: number; failed: number; oldest_seconds: number }
  provider: { delivered: number; bounced: number; complained: number; indeterminate: number }
  reconciliation: { retry: number; dead_letter: number; indeterminate: number }
  slo: { inbound_failure_ratio: number; send_failure_ratio: number; bounce_ratio: number; complaint_ratio: number }
}

defineProps<{ route: PortalRoute; params: Record<string, string>; session: BrowserSession }>()
const page = usePage(() => appApi<ObservabilitySnapshot>('/app/api/admin/observability/webmail-postal'), { cacheKey: 'admin-webmail-postal-observability' })
const source = computed(() => page.stale.value ? 'stale' : 'live')
const pct = (value: number) => (value * 100).toFixed(2) + '%'
</script>

<template>
  <div>
    <PageHeader title="Webmail / Postal observability" eyebrow="Platform operations"
      description="Operational health for inbound, outbound, provider delivery and reconciliation. Cross-system actions remain governed by Caddy → Kong → Middleware.">
      <button type="button" class="kp-button" :disabled="page.status.value === 'loading'" @click="page.reload">Refresh</button>
    </PageHeader>
    <LoadingState v-if="page.status.value === 'loading'" label="Loading Webmail and Postal health…" />
    <ForbiddenState v-else-if="page.status.value === 'forbidden'" reason="platform-admin" :request-id="page.failure.value?.requestId" :code="page.failure.value?.code" />
    <ErrorState v-else-if="page.failure.value" :failure="page.failure.value" @retry="page.reload" />
    <div v-else-if="page.data.value" class="kp-stack">
      <section class="kp-grid" aria-label="Webmail Postal health">
        <MetricCard label="Inbound accepted" :value="page.data.value.inbound.accepted" detail="Persisted inbound messages" :source="source" />
        <MetricCard label="Inbound quarantined" :value="page.data.value.inbound.quarantined" detail="Held by policy/auth checks" :source="source" />
        <MetricCard label="Outbound active" :value="page.data.value.outbound.active" detail="Pending, sending or retrying" :source="source" />
        <MetricCard label="Outbound failed" :value="page.data.value.outbound.failed" detail="Exhausted send work" :source="source" />
        <MetricCard label="Oldest queue item" :value="page.data.value.outbound.oldest_seconds + 's'" detail="Active email outbox age" :source="source" />
        <MetricCard label="Reconciliation unresolved" :value="page.data.value.reconciliation.retry + page.data.value.reconciliation.dead_letter + page.data.value.reconciliation.indeterminate" detail="Retry, dead-letter or indeterminate" :source="source" />
        <MetricCard label="Inbound failure ratio" :value="pct(page.data.value.slo.inbound_failure_ratio)" detail="Current durable-state snapshot" :source="source" />
        <MetricCard label="Bounce ratio" :value="pct(page.data.value.slo.bounce_ratio)" detail="Provider message outcomes" :source="source" />
      </section>
      <PanelCard title="Governed architecture" eyebrow="Authority" source="live">
        <p>{{ page.data.value.architecture }}</p>
        <p>Grafana/Prometheus/Alertmanager/Tempo/Loki are read/observe infrastructure. Business-system writes are never issued directly from this page.</p>
      </PanelCard>
    </div>
  </div>
</template>
