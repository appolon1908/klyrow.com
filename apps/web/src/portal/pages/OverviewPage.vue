<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { appApi, type BrowserSession } from '../../api'
import type { PortalRoute } from '../routes'
import type { DashboardData, DomainClaim, OnboardingState, ProvisioningStatus, SenderIdentity } from '../types'
import { settle } from '../composables/usePage'
import type { PortalFailure } from '../errors'
import { readTenantState, writeTenantState } from '../state'
import PageHeader from '../components/PageHeader.vue'
import MetricCard from '../components/MetricCard.vue'
import PanelCard from '../components/PanelCard.vue'
import StatusBadge from '../components/StatusBadge.vue'
import SourceBadge, { type DataSource } from '../components/SourceBadge.vue'
import LoadingState from '../components/LoadingState.vue'
import ErrorState from '../components/ErrorState.vue'
import DegradedState from '../components/DegradedState.vue'
import UnavailableState from '../components/UnavailableState.vue'
import DataTable from '../components/DataTable.vue'
import SafeText from '../components/SafeText.vue'

defineProps<{ route: PortalRoute; params: Record<string, string>; session: BrowserSession }>()

interface Loaded {
  dashboard: { value: DashboardData | null; failure: PortalFailure | null }
  onboarding: { value: OnboardingState | null; failure: PortalFailure | null }
  provisioning: { value: ProvisioningStatus | null; failure: PortalFailure | null }
  domains: { value: DomainClaim[] | null; failure: PortalFailure | null }
  senders: { value: SenderIdentity[] | null; failure: PortalFailure | null }
}
const loaded = ref<Loaded | null>(readTenantState<Loaded>('overview') || null)
const loading = ref(!loaded.value)
const stale = ref(!!loaded.value)

function sourceOf(part: { value: unknown; failure: PortalFailure | null }): DataSource {
  if (part.failure) return part.failure.kind === 'forbidden' ? 'forbidden' : 'degraded'
  return stale.value ? 'stale' : 'live'
}

async function load() {
  loading.value = !loaded.value
  const result = await settle({
    dashboard: () => appApi<DashboardData>('/app/api/dashboard'),
    onboarding: () => appApi<OnboardingState>('/app/api/onboarding'),
    provisioning: () => appApi<ProvisioningStatus>('/app/api/provisioning/postal'),
    domains: () => appApi<DomainClaim[]>('/app/api/domains'),
    senders: () => appApi<SenderIdentity[]>('/app/api/senders'),
  })
  loaded.value = result
  writeTenantState('overview', result)
  stale.value = false
  loading.value = false
}
onMounted(load)

const dashboard = computed(() => loaded.value?.dashboard.value || null)
const metrics = computed(() => dashboard.value?.metrics || null)
const quotaUsed = computed(() => metrics.value ? Math.min(100, Math.round((metrics.value.sent_24h / Math.max(1, metrics.value.quota)) * 100)) : 0)
const verifiedDomains = computed(() => (loaded.value?.domains.value || []).filter(item => ['VERIFIED', 'SENDING_ENABLED'].includes(item.state)))
const readiness = computed(() => {
  if (!loaded.value) return null
  const domainsOk = verifiedDomains.value.length > 0
  const sendersOk = (loaded.value.senders.value || []).some(item => item.status === 'ACTIVE' && item.verified)
  const provisioningOk = loaded.value.provisioning.value?.state === 'READY'
  return { domainsOk, sendersOk, provisioningOk, ready: domainsOk && sendersOk && provisioningOk }
})
const onboarding = computed(() => loaded.value?.onboarding.value || dashboard.value?.onboarding || null)
const checklist = computed(() => Object.entries(onboarding.value?.checklist || {}))
const checklistDone = computed(() => checklist.value.filter(([, done]) => done).length)
const allFailed = computed(() => !!loaded.value && Object.values(loaded.value).every(part => part.failure))
const messageColumns = [{ key: 'recipient', label: 'Recipient' }, { key: 'subject', label: 'Subject' }, { key: 'status', label: 'Status' }, { key: 'created_at', label: 'Created' }]
</script>

<template>
  <div>
    <PageHeader title="Overview" eyebrow="Workspace" description="Operational home for this organization. Every card states where its data comes from; nothing is estimated.">
      <button type="button" class="kp-button" :disabled="loading" @click="load">Refresh</button>
      <a class="kp-button--primary" href="/app">Send transactional email</a>
    </PageHeader>

    <LoadingState v-if="loading" label="Loading workspace readiness…" :lines="4" />
    <ErrorState v-else-if="allFailed && loaded" :failure="loaded.dashboard.failure!" @retry="load" />
    <div v-else-if="loaded" class="kp-stack">
      <div v-if="onboarding && !onboarding.completed" class="kp-notice kp-notice--warning" role="status">
        <strong>Finish workspace setup.</strong> {{ checklistDone }} of {{ Math.max(checklist.length, 1) }} checklist steps complete.
        <a href="/onboarding" data-external="true">Continue setup →</a>
      </div>

      <section class="kp-grid" aria-label="Key metrics">
        <MetricCard label="Sent · last 24h" :value="metrics ? metrics.sent_24h : null" :detail="metrics ? `${quotaUsed}% of daily quota (${metrics.quota.toLocaleString()})` : undefined" :source="sourceOf(loaded.dashboard)" />
        <MetricCard label="Delivery rate" :value="metrics ? `${(metrics.delivery_rate * 100).toFixed(1)}%` : null" :detail="metrics ? `${metrics.delivered.toLocaleString()} delivered · ${metrics.bounced.toLocaleString()} bounced` : undefined" :source="sourceOf(loaded.dashboard)" />
        <MetricCard label="Verified domains" :value="loaded.domains.value ? `${verifiedDomains.length}/${loaded.domains.value.length}` : null" detail="DNS ownership verified" :source="sourceOf(loaded.domains)" />
        <MetricCard label="Outbox" :value="metrics ? metrics.outbox_active : null" :detail="metrics ? `${metrics.outbox_failed.toLocaleString()} failed` : undefined" :source="sourceOf(loaded.dashboard)" />
      </section>

      <div class="kp-grid--2">
        <PanelCard title="Sending readiness" eyebrow="Readiness" :source="readiness ? (stale ? 'stale' : 'live') : 'unavailable'">
          <ul v-if="readiness" class="kp-checklist">
            <li :data-done="readiness.domainsOk"><StatusBadge :value="readiness.domainsOk ? 'ready' : 'pending'" /> Verified sending domain <a href="/app/email/domains">Domains</a></li>
            <li :data-done="readiness.sendersOk"><StatusBadge :value="readiness.sendersOk ? 'ready' : 'pending'" /> Active verified sender <a href="/app/email/senders">Senders</a></li>
            <li :data-done="readiness.provisioningOk"><StatusBadge :value="loaded.provisioning.value?.state || 'unknown'" /> Sending infrastructure provisioned <a href="/app/provisioning" data-external="true">Provisioning</a></li>
          </ul>
          <DegradedState v-if="loaded.provisioning.failure" message="Provisioning status could not be read." :request-id="loaded.provisioning.failure.requestId" :code="loaded.provisioning.failure.code" />
          <DegradedState v-if="loaded.domains.failure" message="Domain claims could not be read." :request-id="loaded.domains.failure.requestId" :code="loaded.domains.failure.code" />
          <DegradedState v-if="loaded.senders.failure" message="Sender identities could not be read." :request-id="loaded.senders.failure.requestId" :code="loaded.senders.failure.code" />
        </PanelCard>

        <PanelCard title="Organization summary" eyebrow="Organization" :source="sourceOf(loaded.dashboard)">
          <dl class="kp-dl">
            <div><dt>Organization</dt><dd><SafeText :value="session.tenant_id" /></dd></div>
            <div><dt>Your role</dt><dd><SafeText :value="session.role" /></dd></div>
            <div><dt>Contacts</dt><dd>{{ metrics ? metrics.contacts.toLocaleString() : 'Not available' }}</dd></div>
            <div><dt>Campaign definitions</dt><dd>{{ metrics ? metrics.campaigns.toLocaleString() : 'Not available' }}</dd></div>
            <div><dt>Suppressions</dt><dd>{{ metrics ? metrics.suppressions.toLocaleString() : 'Not available' }}</dd></div>
            <div><dt>Messages recorded</dt><dd>{{ metrics ? metrics.messages_total.toLocaleString() : 'Not available' }}</dd></div>
          </dl>
          <DegradedState v-if="loaded.dashboard.failure" message="Dashboard metrics could not be read." :request-id="loaded.dashboard.failure.requestId" :code="loaded.dashboard.failure.code" />
        </PanelCard>
      </div>

      <div class="kp-grid--2">
        <PanelCard title="Plan and usage" eyebrow="Billing" :source="sourceOf(loaded.dashboard)">
          <p v-if="metrics">Daily quota <strong>{{ metrics.quota.toLocaleString() }}</strong> messages · {{ quotaUsed }}% used in the last 24 hours.</p>
          <div v-if="metrics" class="kp-progress" role="progressbar" aria-label="Daily quota used" :aria-valuenow="quotaUsed" aria-valuemin="0" aria-valuemax="100"><span :style="{ width: `${quotaUsed}%` }"></span></div>
          <p class="kp-muted">Subscription, invoice and plan detail have no browser API yet. <a href="/app/billing/plan">Billing</a></p>
        </PanelCard>
        <PanelCard title="Deliverability and incidents" eyebrow="Health" source="unavailable">
          <UnavailableState title="Deliverability summary" dependency="No browser API exists yet. Required contract: GET /app/api/deliverability/summary (DNS, TLS, PTR, reputation, active incidents)." :alternatives="[{ label: 'Domain states', href: '/app/deliverability' }]" />
        </PanelCard>
      </div>

      <PanelCard title="Recent message activity" eyebrow="Traffic" :source="sourceOf(loaded.dashboard)">
        <template #actions><a class="kp-button" href="/app/email/messages">All messages</a></template>
        <DataTable caption="Recent messages" :columns="messageColumns" :rows="dashboard?.recent_messages || []" empty-message="No messages recorded yet.">
          <template #cell-status="{ value }"><StatusBadge :value="String(value)" /></template>
          <template #cell-created_at="{ value }"><time :datetime="String(value)">{{ new Date(String(value)).toLocaleString() }}</time></template>
          <template #cell-subject="{ row }"><a :href="`/app/email/messages/${encodeURIComponent(String(row.id))}`"><SafeText :value="row.subject" fallback="(no subject)" /></a></template>
        </DataTable>
      </PanelCard>

      <PanelCard title="Quick actions" eyebrow="Shortcuts">
        <div class="kp-quick-actions">
          <a class="kp-button" href="/app/mail" data-external="true">Open webmail</a>
          <a class="kp-button" href="/app/email/domains">Manage domains</a>
          <a class="kp-button" href="/app/email/senders">Manage senders</a>
          <a class="kp-button" href="/app/developer/api-keys">Developer credentials</a>
          <a class="kp-button" href="/app/support">Support</a>
        </div>
        <p class="kp-muted" style="margin-top:12px">
          <SourceBadge :source="sourceOf(loaded.onboarding)" /> Onboarding progress: {{ onboarding ? (onboarding.completed ? 'complete' : `step ${onboarding.step}`) : 'not started' }}
        </p>
      </PanelCard>
    </div>
  </div>
</template>
