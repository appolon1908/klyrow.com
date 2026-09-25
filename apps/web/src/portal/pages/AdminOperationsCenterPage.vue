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

interface Center {
  generated_at: string
  overall_health: string
  suites: { users: string; email: string; billing: string; system: string }
  architecture: string[]
  questions_answered: string[]
}
interface Users { users:{total:number;enabled:number;disabled:number}; sessions:{active:number}; health:string }
interface Billing { payment_attempts:{total:number;captured:number;failed:number}; ledger:{invoices:number;payments:number;refunds:number}; health:string }
interface System { middleware_commands:{total:number;failed:number}; integration_outbox:{pending_or_retry:number;dead_letter:number}; health:string }

defineProps<{ route: PortalRoute; params: Record<string,string>; session: BrowserSession }>()
const page = usePage(async () => {
  const [center, users, billing, system] = await Promise.all([
    appApi<Center>('/app/api/admin/observability/operations-center'),
    appApi<Users>('/app/api/admin/observability/users'),
    appApi<Billing>('/app/api/admin/observability/billing'),
    appApi<System>('/app/api/admin/observability/system'),
  ])
  return { center, users, billing, system }
}, { cacheKey: 'admin-operations-center' })
const source = computed(() => page.stale.value ? 'stale' : 'live')
const label = (v:string) => v === 'critical' ? 'Critical' : v === 'attention' ? 'Attention' : 'Healthy'
</script>

<template>
  <div>
    <PageHeader title="Operations Center" eyebrow="Platform administration"
      description="Unified operational view across Users, Email, Billing and Middleware. Every cross-system action remains governed by Caddy → Kong → Middleware.">
      <a class="kp-button" href="/admin/observability">Email detail</a>
      <button class="kp-button" type="button" :disabled="page.status.value === 'loading'" @click="page.reload">Refresh</button>
    </PageHeader>
    <LoadingState v-if="page.status.value === 'loading'" label="Loading platform operations…" />
    <ForbiddenState v-else-if="page.status.value === 'forbidden'" reason="platform-admin" :request-id="page.failure.value?.requestId" :code="page.failure.value?.code" />
    <ErrorState v-else-if="page.failure.value" :failure="page.failure.value" @retry="page.reload" />
    <div v-else-if="page.data.value" class="kp-stack">
      <PanelCard title="Platform health" eyebrow="Executive status" :source="source">
        <section class="kp-grid">
          <MetricCard label="Overall" :value="label(page.data.value.center.overall_health)" detail="Highest active suite severity" :source="source" />
          <MetricCard label="Users" :value="label(page.data.value.center.suites.users)" detail="Identity and sessions" :source="source" />
          <MetricCard label="Email" :value="label(page.data.value.center.suites.email)" detail="Webmail and Postal" :source="source" />
          <MetricCard label="Billing" :value="label(page.data.value.center.suites.billing)" detail="Attempts and ledger" :source="source" />
          <MetricCard label="Middleware" :value="label(page.data.value.center.suites.system)" detail="Commands and integration outbox" :source="source" />
        </section>
      </PanelCard>

      <PanelCard title="Users & access" eyebrow="Identity" :source="source">
        <section class="kp-grid">
          <MetricCard label="Users" :value="page.data.value.users.users.total" detail="Known application identities" :source="source" />
          <MetricCard label="Enabled" :value="page.data.value.users.users.enabled" detail="Enabled identities" :source="source" />
          <MetricCard label="Disabled" :value="page.data.value.users.users.disabled" detail="Disabled identities" :source="source" />
          <MetricCard label="Active sessions" :value="page.data.value.users.sessions.active" detail="Non-revoked browser sessions" :source="source" />
        </section>
      </PanelCard>

      <PanelCard title="Billing operations" eyebrow="Financial control plane" :source="source">
        <section class="kp-grid">
          <MetricCard label="Payment attempts" :value="page.data.value.billing.payment_attempts.total" detail="Provider-neutral attempts" :source="source" />
          <MetricCard label="Captured" :value="page.data.value.billing.payment_attempts.captured" detail="Captured attempts" :source="source" />
          <MetricCard label="Failed" :value="page.data.value.billing.payment_attempts.failed" detail="Failed/canceled/expired" :source="source" />
          <MetricCard label="Invoices" :value="page.data.value.billing.ledger.invoices" detail="Ledger invoices" :source="source" />
          <MetricCard label="Payments" :value="page.data.value.billing.ledger.payments" detail="Recorded payments" :source="source" />
          <MetricCard label="Refunds" :value="page.data.value.billing.ledger.refunds" detail="Recorded refunds" :source="source" />
        </section>
      </PanelCard>

      <PanelCard title="Middleware & integrations" eyebrow="Command authority" :source="source">
        <section class="kp-grid">
          <MetricCard label="Commands" :value="page.data.value.system.middleware_commands.total" detail="Durable Middleware commands" :source="source" />
          <MetricCard label="Command failures" :value="page.data.value.system.middleware_commands.failed" detail="Failed command operations" :source="source" />
          <MetricCard label="Pending / retry" :value="page.data.value.system.integration_outbox.pending_or_retry" detail="Integration outbox work" :source="source" />
          <MetricCard label="Dead letter" :value="page.data.value.system.integration_outbox.dead_letter" detail="Operator intervention required" :source="source" />
        </section>
      </PanelCard>

      <PanelCard title="What this console answers" eyebrow="Operator questions" :source="source">
        <ul><li v-for="question in page.data.value.center.questions_answered" :key="question">{{ question }}</li></ul>
      </PanelCard>

      <PanelCard title="Governed architecture" eyebrow="No bypass" source="live">
        <p><strong>{{ page.data.value.center.architecture.join(' → ') }}</strong></p>
        <p>Users, email, billing and administrative operations share one cross-system authority: Middleware. Monitoring and UI surfaces remain read-only unless a separately authorized command is sent through that path.</p>
      </PanelCard>
    </div>
  </div>
</template>
