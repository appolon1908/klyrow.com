<script setup lang="ts">
import { computed, ref } from 'vue'
import { appApi, type BrowserSession } from '../../api'
import type { PortalRoute } from '../routes'
import type { DomainClaim } from '../types'
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
import UnavailableState from '../components/UnavailableState.vue'
import RequestIdentifiers from '../components/RequestIdentifiers.vue'
import TabList from '../components/TabList.vue'

const props = defineProps<{ route: PortalRoute; params: Record<string, string>; session: BrowserSession }>()
const canManage = computed(() => isManagementRole(props.session))
const page = usePage(() => appApi<DomainClaim[]>('/app/api/domains'), { cacheKey: 'domains' })
const claim = computed(() => (page.data.value || []).find(item => item.id === props.params.id) || null)
const verified = computed(() => !!claim.value && ['VERIFIED', 'SENDING_ENABLED'].includes(claim.value.state))
const blockedReason = computed(() => {
  if (!claim.value) return ''
  if (claim.value.suspended_at) return `Suspended on ${new Date(claim.value.suspended_at).toLocaleString()}; sending is blocked until an operator lifts the suspension.`
  if (claim.value.state === 'DNS_REQUIRED') return 'Ownership has not been proven: publish the ownership TXT record and run verification.'
  return ''
})
const tab = ref('dns')
const tabs = [{ id: 'dns', label: 'DNS records' }, { id: 'dkim', label: 'DKIM' }, { id: 'evidence', label: 'Evidence' }]
const verifying = ref(false)
const failure = ref<PortalFailure | null>(null)
const deliverabilityPage = computed(() => props.route.name === 'deliverability-domain')

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
</script>

<template>
  <div>
    <PageHeader :title="claim ? claim.domain : 'Domain'" :eyebrow="deliverabilityPage ? 'Deliverability' : 'Email operations'">
      <a class="kp-button" :href="deliverabilityPage ? '/app/deliverability' : '/app/email/domains'">Back</a>
      <button v-if="canManage && claim && !verified" type="button" class="kp-button--primary" :disabled="verifying" @click="verify">{{ verifying ? 'Checking DNS…' : 'Verify ownership' }}</button>
    </PageHeader>
    <LoadingState v-if="page.status.value === 'loading'" label="Loading domain…" />
    <ForbiddenState v-else-if="page.status.value === 'forbidden'" reason="server" :request-id="page.failure.value?.requestId" :code="page.failure.value?.code" />
    <ErrorState v-else-if="page.failure.value" :failure="page.failure.value" @retry="page.reload" />
    <EmptyState v-else-if="!claim" title="Domain not found" description="This claim does not exist in the current organization." />
    <div v-else class="kp-stack">
      <div v-if="failure" class="kp-notice kp-notice--danger" role="alert">
        <strong>Verification did not succeed.</strong> {{ describeFailure(failure) }}
        <RequestIdentifiers :request-id="failure.requestId" :correlation-id="failure.correlationId" :code="failure.code" :status="failure.status" />
      </div>
      <div v-if="blockedReason" class="kp-notice kp-notice--warning" role="status">{{ blockedReason }}</div>
      <section class="kp-grid" aria-label="Domain status">
        <article class="kp-metric"><span class="kp-metric__label">Claim state</span><StatusBadge :value="claim.state" /><span class="kp-metric__detail">{{ verified ? 'Ownership proven by DNS' : 'Awaiting DNS proof' }}</span></article>
        <article class="kp-metric"><span class="kp-metric__label">Verified</span><strong class="kp-metric__value" :class="{ 'kp-metric__value--muted': !claim.verified_at }">{{ claim.verified_at ? new Date(claim.verified_at).toLocaleDateString() : 'Not yet' }}</strong></article>
        <article class="kp-metric"><span class="kp-metric__label">DKIM version</span><strong class="kp-metric__value">{{ claim.dkim_version ?? 1 }}</strong><span class="kp-metric__detail">selector {{ claim.dkim_selector || '—' }}</span></article>
      </section>
      <TabList v-model="tab" :tabs="tabs" label="Domain sections" />
      <PanelCard v-if="tab === 'dns'" id="panel-dns" title="DNS records" eyebrow="Guidance" source="live">
        <dl class="kp-dl">
          <div><dt>Ownership TXT</dt><dd><code class="kp-code">_klyrow-verification.{{ claim.domain }}</code><br><span class="kp-muted">Value <code class="kp-code">klyrow=…</code> was shown once when the domain was claimed; re-claiming is required to obtain a new challenge.</span></dd></div>
          <div><dt>SPF</dt><dd><code class="kp-code">{{ claim.domain }}</code> TXT <code class="kp-code">v=spf1 include:spf.klyrow.com -all</code></dd></div>
          <div><dt>DKIM</dt><dd><code class="kp-code">{{ claim.dkim_selector || 'selector' }}._domainkey.{{ claim.domain }}</code> — public key record published by Klyrow provisioning</dd></div>
          <div><dt>DMARC</dt><dd><code class="kp-code">_dmarc.{{ claim.domain }}</code> TXT policy recommended (<code class="kp-code">v=DMARC1; p=quarantine</code> or stricter)</dd></div>
          <div><dt>Return path</dt><dd><code class="kp-code">{{ claim.return_path || '—' }}</code></dd></div>
          <div><dt>Tracking domain</dt><dd><code class="kp-code">{{ claim.tracking_domain || '—' }}</code></dd></div>
        </dl>
      </PanelCard>
      <PanelCard v-if="tab === 'dkim'" id="panel-dkim" title="DKIM" eyebrow="Signing" source="live">
        <p>Active selector <code class="kp-code">{{ claim.dkim_selector || '—' }}</code>, version {{ claim.dkim_version ?? 1 }}.</p>
        <UnavailableState title="DKIM key history and rotation" dependency="No browser API exists yet. Required contract: GET /app/api/domains/{id} (key versions, public records, rotation) and POST /app/api/domains/{id}/dkim/rotate." />
      </PanelCard>
      <PanelCard v-if="tab === 'evidence'" id="panel-evidence" title="Deliverability evidence" eyebrow="SPF · DMARC · PTR · TLS" source="unavailable">
        <UnavailableState title="DNS, PTR and TLS evidence" dependency="No browser API exists yet. Required contract: GET /app/api/domains/{id} returning SPF/DMARC/PTR/TLS check results with checked-at timestamps." :alternatives="[{ label: 'Domain list', href: '/app/email/domains' }]" />
      </PanelCard>
    </div>
  </div>
</template>
