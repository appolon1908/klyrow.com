<script setup lang="ts">
import { computed, ref } from 'vue'
import { appApi, type BrowserSession } from '../../api'
import type { PortalRoute } from '../routes'
import type { DomainDetail } from '../types'
import { isManagementRole } from '../access'
import { usePage } from '../composables/usePage'
import { describeFailure, normalizeFailure, type PortalFailure } from '../errors'
import { notify } from '../toasts'
import PageHeader from '../components/PageHeader.vue'
import PanelCard from '../components/PanelCard.vue'
import StatusBadge from '../components/StatusBadge.vue'
import LoadingState from '../components/LoadingState.vue'
import EmptyState from '../components/EmptyState.vue'
import ErrorState from '../components/ErrorState.vue'
import ForbiddenState from '../components/ForbiddenState.vue'
import RequestIdentifiers from '../components/RequestIdentifiers.vue'
import TabList from '../components/TabList.vue'

const props = defineProps<{ route: PortalRoute; params: Record<string, string>; session: BrowserSession }>()
const canManage = computed(() => isManagementRole(props.session))
const page = usePage(() => appApi<DomainDetail>(`/app/api/domains/${encodeURIComponent(props.params.id)}`))
const claim = computed(() => page.data.value)
const verified = computed(() => !!claim.value && ['VERIFIED', 'SENDING_ENABLED'].includes(claim.value.state))
const blockedReason = computed(() => {
  if (!claim.value) return ''
  if (claim.value.suspended_at) return `Suspended on ${new Date(claim.value.suspended_at).toLocaleString()}; sending is blocked until the suspension is lifted.`
  if (claim.value.state === 'DNS_REQUIRED') return 'Ownership has not been proven: publish the ownership TXT record and run verification.'
  if (!claim.value.provider_readiness.sending_enabled) return 'Domain ownership is known, but provider sending readiness is not enabled.'
  return ''
})
const tab = ref('dns')
const tabs = [{ id: 'dns', label: 'DNS records' }, { id: 'dkim', label: 'DKIM' }, { id: 'evidence', label: 'Evidence' }]
const verifying = ref(false)
const checking = ref(false)
const failure = ref<PortalFailure | null>(null)
const deliverabilityPage = computed(() => props.route.name === 'deliverability-domain')
const evidence = computed(() => claim.value?.deliverability)
const evidenceSource = computed(() => evidence.value?.source === 'durable_snapshot' ? (evidence.value.stale ? 'stale' : 'live') : 'unavailable')

async function verify() {
  if (!claim.value) return
  verifying.value = true
  failure.value = null
  try {
    await appApi(`/app/api/domains/${encodeURIComponent(claim.value.id)}/verify`, { method: 'POST', body: '{}' })
    notify('success', `${claim.value.domain} verified.`)
    await page.reload()
  } catch (error) {
    failure.value = normalizeFailure(error)
  } finally {
    verifying.value = false
  }
}

async function checkDeliverability() {
  if (!claim.value) return
  checking.value = true
  failure.value = null
  try {
    await appApi(`/app/api/deliverability/domains/${encodeURIComponent(claim.value.id)}/check`, {
      method: 'POST',
      headers: { 'Idempotency-Key': crypto.randomUUID() },
      body: '{}',
    })
    notify('success', 'Deliverability evidence refreshed.')
    await page.reload()
  } catch (error) {
    failure.value = normalizeFailure(error)
  } finally {
    checking.value = false
  }
}

function evidenceLabel(value: boolean | null | undefined) {
  return value === true ? 'pass' : value === false ? 'fail' : 'unknown'
}
</script>

<template>
  <div>
    <PageHeader :title="claim ? claim.domain : 'Domain'" :eyebrow="deliverabilityPage ? 'Deliverability' : 'Email operations'">
      <a class="kp-button" :href="deliverabilityPage ? '/app/deliverability' : '/app/email/domains'">Back</a>
      <button v-if="canManage && claim && !verified" type="button" class="kp-button--primary" :disabled="verifying" @click="verify">{{ verifying ? 'Checking DNS…' : 'Verify ownership' }}</button>
      <button v-if="canManage && claim && verified" type="button" class="kp-button--primary" :disabled="checking" @click="checkDeliverability">{{ checking ? 'Checking…' : 'Run deliverability check' }}</button>
    </PageHeader>
    <LoadingState v-if="page.status.value === 'loading'" label="Loading domain…" />
    <ForbiddenState v-else-if="page.status.value === 'forbidden'" reason="server" :request-id="page.failure.value?.requestId" :code="page.failure.value?.code" />
    <ErrorState v-else-if="page.failure.value" :failure="page.failure.value" @retry="page.reload" />
    <EmptyState v-else-if="!claim" title="Domain not found" description="This claim does not exist in the current organization." />
    <div v-else class="kp-stack">
      <div v-if="failure" class="kp-notice kp-notice--danger" role="alert">
        <strong>Operation did not succeed.</strong> {{ describeFailure(failure) }}
        <RequestIdentifiers :request-id="failure.requestId" :correlation-id="failure.correlationId" :code="failure.code" :status="failure.status" />
      </div>
      <div v-if="blockedReason" class="kp-notice kp-notice--warning" role="status">{{ blockedReason }}</div>
      <div v-if="evidence?.stale && evidence.source === 'durable_snapshot'" class="kp-notice kp-notice--warning" role="status">Deliverability evidence is older than 24 hours. Run a new check before treating it as current.</div>
      <section class="kp-grid" aria-label="Domain status">
        <article class="kp-metric"><span class="kp-metric__label">Claim state</span><StatusBadge :value="claim.state" /><span class="kp-metric__detail">{{ verified ? 'Ownership proven by DNS' : 'Awaiting DNS proof' }}</span></article>
        <article class="kp-metric"><span class="kp-metric__label">Sending readiness</span><StatusBadge :value="claim.provider_readiness.sending_enabled" /><span class="kp-metric__detail">provider {{ claim.provider_readiness.status }}</span></article>
        <article class="kp-metric"><span class="kp-metric__label">Inbound readiness</span><StatusBadge :value="claim.provider_readiness.inbound_enabled" /><span class="kp-metric__detail">{{ claim.provider_readiness.inbound_enabled ? 'Inbound route enabled' : 'Not enabled' }}</span></article>
      </section>
      <TabList v-model="tab" :tabs="tabs" label="Domain sections" />
      <PanelCard v-if="tab === 'dns'" id="panel-dns" title="DNS records" eyebrow="Guidance" source="live">
        <dl class="kp-dl">
          <div><dt>Ownership TXT</dt><dd><code class="kp-code">_klyrow-verification.{{ claim.domain }}</code><br><span class="kp-muted">The challenge value is shown only when a domain is claimed.</span></dd></div>
          <div><dt>SPF</dt><dd><code class="kp-code">{{ claim.domain }}</code> TXT <code class="kp-code">v=spf1 include:spf.klyrow.com -all</code></dd></div>
          <div><dt>DKIM</dt><dd><code class="kp-code">{{ claim.dkim.selector || 'selector' }}._domainkey.{{ claim.domain }}</code></dd></div>
          <div><dt>DMARC</dt><dd><code class="kp-code">_dmarc.{{ claim.domain }}</code> TXT policy recommended</dd></div>
          <div><dt>Return path</dt><dd><code class="kp-code">{{ claim.dns.return_path || '—' }}</code></dd></div>
          <div><dt>Tracking domain</dt><dd><code class="kp-code">{{ claim.dns.tracking_domain || '—' }}</code></dd></div>
        </dl>
      </PanelCard>
      <PanelCard v-if="tab === 'dkim'" id="panel-dkim" title="DKIM" eyebrow="Signing" source="live">
        <p>Active selector <code class="kp-code">{{ claim.dkim.selector || '—' }}</code>, version {{ claim.dkim.version }}.</p>
        <ol v-if="claim.dkim.history.length" class="kp-list" aria-label="DKIM key history">
          <li v-for="key in claim.dkim.history" :key="`${key.selector}:${key.version}`"><code class="kp-code">{{ key.selector }}</code> v{{ key.version }} · <StatusBadge :value="key.active" /> · {{ new Date(key.created_at).toLocaleDateString() }}</li>
        </ol>
        <p v-else class="kp-muted">No rotation history is recorded yet; the claim selector is the current authority.</p>
      </PanelCard>
      <PanelCard v-if="tab === 'evidence'" id="panel-evidence" title="Deliverability evidence" eyebrow="SPF · DKIM · DMARC · MX · PTR · TLS" :source="evidenceSource">
        <div v-if="evidence?.source === 'none'" class="kp-notice kp-notice--warning" role="status">No durable deliverability snapshot exists yet.</div>
        <dl v-else class="kp-dl">
          <div><dt>Checked</dt><dd>{{ evidence?.checked_at ? new Date(evidence.checked_at).toLocaleString() : 'Unknown' }}</dd></div>
          <div><dt>SPF</dt><dd><StatusBadge :value="evidenceLabel(evidence?.spf)" /></dd></div>
          <div><dt>DKIM</dt><dd><StatusBadge :value="evidenceLabel(evidence?.dkim)" /></dd></div>
          <div><dt>DMARC</dt><dd><StatusBadge :value="evidenceLabel(evidence?.dmarc)" /></dd></div>
          <div><dt>MX</dt><dd><StatusBadge :value="evidenceLabel(evidence?.mx)" /></dd></div>
          <div><dt>PTR</dt><dd><StatusBadge :value="evidenceLabel(evidence?.ptr)" /></dd></div>
          <div><dt>TLS</dt><dd><StatusBadge :value="evidenceLabel(evidence?.tls)" /></dd></div>
        </dl>
        <ul v-if="evidence?.alerts.length" class="kp-list" aria-label="Deliverability alerts">
          <li v-for="(alert, index) in evidence.alerts" :key="`${alert.code || 'alert'}:${index}`"><strong>{{ alert.severity || 'warning' }}</strong> · {{ alert.code || 'deliverability_alert' }}</li>
        </ul>
      </PanelCard>
    </div>
  </div>
</template>
