<script setup lang="ts">
import { computed, ref } from 'vue'
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
  health: { inbound: string; outbound: string; queue: string; reconciliation: string }
  thresholds: { inbound_failure_ratio: number; send_failure_ratio: number; queue_age_seconds: number; bounce_ratio: number; complaint_ratio: number }
  slo: { inbound_failure_ratio: number; send_failure_ratio: number; bounce_ratio: number; complaint_ratio: number }
}

defineProps<{ route: PortalRoute; params: Record<string, string>; session: BrowserSession }>()
const page = usePage(() => appApi<ObservabilitySnapshot>('/app/api/admin/observability/webmail-postal'), { cacheKey: 'admin-webmail-postal-observability' })
const source = computed(() => page.stale.value ? 'stale' : 'live')
const pct = (value: number) => (value * 100).toFixed(2) + '%'
const unresolved = computed(() => {
  const r = page.data.value?.reconciliation
  return r ? r.retry + r.dead_letter + r.indeterminate : 0
})
const traceId = ref('')
const traceBusy = ref(false)
const traceError = ref('')
const traceResult = ref<{ correlation_id: string; found: boolean; path: string[]; timeline: Array<{at:string;layer:string;state:string;kind:string;attempts:number}>; privacy: string } | null>(null)
async function inspectTrace() {
  const value = traceId.value.trim()
  if (!value) return
  traceBusy.value = true; traceError.value = ''; traceResult.value = null
  try {
    traceResult.value = await appApi('/app/api/admin/observability/webmail-postal/traces/' + encodeURIComponent(value))
  } catch (error) {
    traceError.value = error instanceof Error ? error.message : 'trace_lookup_failed'
  } finally { traceBusy.value = false }
}
const healthLabel = (value: string) => value === 'critical' ? 'Critical' : value === 'attention' ? 'Attention' : 'Healthy'
</script>

<template>
  <div>
    <PageHeader title="Webmail & Postal" eyebrow="Observability"
      description="Production operations for mail flow, delivery health and reconciliation. Read-only telemetry; cross-system effects remain Middleware-owned.">
      <button type="button" class="kp-button" :disabled="page.status.value === 'loading'" @click="page.reload">Refresh</button>
    </PageHeader>

    <LoadingState v-if="page.status.value === 'loading'" label="Loading mail operations…" />
    <ForbiddenState v-else-if="page.status.value === 'forbidden'" reason="platform-admin" :request-id="page.failure.value?.requestId" :code="page.failure.value?.code" />
    <ErrorState v-else-if="page.failure.value" :failure="page.failure.value" @retry="page.reload" />

    <div v-else-if="page.data.value" class="kp-stack">
      <PanelCard title="Service health" eyebrow="Overview" :source="source">
        <section class="kp-grid" aria-label="Service health">
          <MetricCard label="Inbound" :value="healthLabel(page.data.value.health.inbound)" :detail="pct(page.data.value.slo.inbound_failure_ratio) + ' failure ratio'" :source="source" />
          <MetricCard label="Outbound" :value="healthLabel(page.data.value.health.outbound)" :detail="pct(page.data.value.slo.send_failure_ratio) + ' failure ratio'" :source="source" />
          <MetricCard label="Queue" :value="healthLabel(page.data.value.health.queue)" :detail="page.data.value.outbound.oldest_seconds + 's oldest item'" :source="source" />
          <MetricCard label="Reconciliation" :value="healthLabel(page.data.value.health.reconciliation)" :detail="unresolved + ' unresolved'" :source="source" />
        </section>
      </PanelCard>

      <PanelCard title="Mail flow" eyebrow="Inbound & outbound" :source="source">
        <section class="kp-grid" aria-label="Mail flow">
          <MetricCard label="Inbound accepted" :value="page.data.value.inbound.accepted" detail="Persisted successfully" :source="source" />
          <MetricCard label="Quarantined" :value="page.data.value.inbound.quarantined" detail="Held by policy or authentication" :source="source" />
          <MetricCard label="Rejected" :value="page.data.value.inbound.rejected" detail="Rejected before Inbox projection" :source="source" />
          <MetricCard label="Outbound active" :value="page.data.value.outbound.active" detail="Pending, sending or retrying" :source="source" />
          <MetricCard label="Outbound failed" :value="page.data.value.outbound.failed" detail="Exhausted send work" :source="source" />
          <MetricCard label="Queue age" :value="page.data.value.outbound.oldest_seconds + 's'" detail="Oldest active outbound item" :source="source" />
        </section>
      </PanelCard>

      <PanelCard title="Delivery outcomes" eyebrow="Postal evidence" :source="source">
        <section class="kp-grid" aria-label="Delivery outcomes">
          <MetricCard label="Delivered" :value="page.data.value.provider.delivered" detail="Confirmed provider delivery" :source="source" />
          <MetricCard label="Bounced" :value="page.data.value.provider.bounced" :detail="pct(page.data.value.slo.bounce_ratio) + ' aggregate ratio'" :source="source" />
          <MetricCard label="Complaints" :value="page.data.value.provider.complained" :detail="pct(page.data.value.slo.complaint_ratio) + ' aggregate ratio'" :source="source" />
          <MetricCard label="Indeterminate" :value="page.data.value.provider.indeterminate" detail="Requires reconciliation before retry" :source="source" />
        </section>
      </PanelCard>

      <PanelCard title="Reconciliation" eyebrow="Durable recovery" :source="source">
        <section class="kp-grid" aria-label="Reconciliation state">
          <MetricCard label="Retry" :value="page.data.value.reconciliation.retry" detail="Awaiting bounded retry" :source="source" />
          <MetricCard label="Dead letter" :value="page.data.value.reconciliation.dead_letter" detail="Requires operator review" :source="source" />
          <MetricCard label="Indeterminate" :value="page.data.value.reconciliation.indeterminate" detail="Reconcile provider truth first" :source="source" />
        </section>
      </PanelCard>

      <PanelCard title="SLO guardrails" eyebrow="Production objectives" :source="source">
        <section class="kp-grid" aria-label="SLO guardrails">
          <MetricCard label="Inbound failures" :value="pct(page.data.value.slo.inbound_failure_ratio)" :detail="'Target ≤ ' + pct(page.data.value.thresholds.inbound_failure_ratio)" :source="source" />
          <MetricCard label="Send failures" :value="pct(page.data.value.slo.send_failure_ratio)" :detail="'Target ≤ ' + pct(page.data.value.thresholds.send_failure_ratio)" :source="source" />
          <MetricCard label="Queue age" :value="page.data.value.outbound.oldest_seconds + 's'" :detail="'Target < ' + page.data.value.thresholds.queue_age_seconds + 's'" :source="source" />
          <MetricCard label="Bounce ratio" :value="pct(page.data.value.slo.bounce_ratio)" :detail="'Target < ' + pct(page.data.value.thresholds.bounce_ratio)" :source="source" />
          <MetricCard label="Complaint ratio" :value="pct(page.data.value.slo.complaint_ratio)" :detail="'Target < ' + pct(page.data.value.thresholds.complaint_ratio)" :source="source" />
        </section>
      </PanelCard>

      <PanelCard title="Trace explorer" eyebrow="Correlation drill-down" :source="source">
        <form class="kp-inline-actions" @submit.prevent="inspectTrace">
          <label for="trace-correlation"><strong>Correlation ID</strong></label>
          <input id="trace-correlation" v-model="traceId" class="kp-input" autocomplete="off" placeholder="corr-…" maxlength="128" />
          <button class="kp-button" type="submit" :disabled="traceBusy || !traceId.trim()">{{ traceBusy ? 'Inspecting…' : 'Inspect trace' }}</button>
        </form>
        <p v-if="traceError" role="alert">{{ traceError }}</p>
        <div v-if="traceResult" class="kp-stack">
          <p><strong>{{ traceResult.found ? 'Trace evidence found' : 'No local durable evidence found' }}</strong> · {{ traceResult.correlation_id }}</p>
          <ol v-if="traceResult.timeline.length">
            <li v-for="item in traceResult.timeline" :key="item.at + item.layer + item.kind">
              <strong>{{ item.layer }}</strong> — {{ item.kind }} · {{ item.state }} · attempts {{ item.attempts }}
              <small>{{ new Date(item.at).toLocaleString() }}</small>
            </li>
          </ol>
          <p>{{ traceResult.privacy }}</p>
        </div>
      </PanelCard>

      <PanelCard title="Governed request path" eyebrow="Architecture" source="live">
        <p><strong>Client / Browser → Caddy → Kong → Middleware → authorized adapter → Klyrow / Postal</strong></p>
        <p>{{ page.data.value.architecture }}</p>
        <p>Prometheus, Grafana, Alertmanager, Tempo and Loki observe the platform. They do not perform business-system mutations or create alternate provider paths.</p>
        <p class="kp-eyebrow">Snapshot {{ new Date(page.data.value.generated_at).toLocaleString() }}</p>
      </PanelCard>
    </div>
  </div>
</template>
