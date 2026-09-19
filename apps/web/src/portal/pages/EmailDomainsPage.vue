<script setup lang="ts">
import { computed, ref } from 'vue'
import { appApi, type BrowserSession } from '../../api'
import type { PortalRoute } from '../routes'
import type { DomainClaim, DomainClaimCreated } from '../types'
import { isManagementRole } from '../access'
import { usePage } from '../composables/usePage'
import { normalizeFailure, describeFailure, type PortalFailure } from '../errors'
import { notify } from '../toasts'
import PageHeader from '../components/PageHeader.vue'
import DataTable from '../components/DataTable.vue'
import StatusBadge from '../components/StatusBadge.vue'
import LoadingState from '../components/LoadingState.vue'
import EmptyState from '../components/EmptyState.vue'
import ErrorState from '../components/ErrorState.vue'
import ForbiddenState from '../components/ForbiddenState.vue'
import ModalDialog from '../components/ModalDialog.vue'
import FormField from '../components/FormField.vue'
import RequestIdentifiers from '../components/RequestIdentifiers.vue'
import CopyButton from '../components/CopyButton.vue'
import SourceBadge from '../components/SourceBadge.vue'

const props = defineProps<{ route: PortalRoute; params: Record<string, string>; session: BrowserSession }>()
const canManage = computed(() => isManagementRole(props.session))
const page = usePage(() => appApi<DomainClaim[]>('/app/api/domains'), { isEmpty: rows => rows.length === 0, cacheKey: 'domains' })
const columns = [{ key: 'domain', label: 'Domain' }, { key: 'state', label: 'State' }, { key: 'verified_at', label: 'Verified' }, { key: 'created_at', label: 'Claimed' }, { key: 'actions', label: 'Actions' }]

const dialogOpen = ref(false)
const domainInput = ref('')
const formError = ref('')
const submitting = ref(false)
const created = ref<{ domain: string; result: DomainClaimCreated } | null>(null)
const actionFailure = ref<PortalFailure | null>(null)
const verifying = ref('')
const DOMAIN_PATTERN = /^[a-z0-9][a-z0-9.-]+\.[a-z]{2,}$/

function openDialog() { dialogOpen.value = true; formError.value = ''; domainInput.value = ''; actionFailure.value = null }
function closeDialog() { dialogOpen.value = false; created.value = null }

async function claim() {
  const domain = domainInput.value.trim().toLowerCase().replace(/\.$/, '')
  if (!DOMAIN_PATTERN.test(domain)) { formError.value = 'Enter a bare domain such as mail.example.com (no protocol or path).'; return }
  submitting.value = true
  formError.value = ''
  try {
    const result = await appApi<DomainClaimCreated>('/app/api/domains', { method: 'POST', body: JSON.stringify({ domain }) })
    created.value = { domain, result }
    notify('success', `Domain ${domain} claimed. Publish the DNS records, then verify.`)
    await page.reload()
  } catch (error) {
    const failure = normalizeFailure(error)
    formError.value = `${describeFailure(failure)}${failure.requestId ? ` (request ${failure.requestId})` : ''}`
  } finally {
    submitting.value = false
  }
}

async function verify(item: DomainClaim) {
  verifying.value = item.id
  actionFailure.value = null
  try {
    await appApi(`/app/api/domains/${encodeURIComponent(item.id)}/verify`, { method: 'POST', body: '{}' })
    notify('success', `${item.domain} verified.`)
    await page.reload()
  } catch (error) {
    actionFailure.value = normalizeFailure(error)
  } finally {
    verifying.value = ''
  }
}
</script>

<template>
  <div>
    <PageHeader title="Domains" eyebrow="Email operations" description="Sending domains are claimed, then proven through a DNS ownership record. A domain is only shown as verified when the server has confirmed the record.">
      <button type="button" class="kp-button" :disabled="page.status.value === 'loading'" @click="page.reload">Refresh</button>
      <button v-if="canManage" type="button" class="kp-button--primary" @click="openDialog">Claim domain</button>
    </PageHeader>
    <LoadingState v-if="page.status.value === 'loading'" label="Loading domain claims…" />
    <ForbiddenState v-else-if="page.status.value === 'forbidden'" reason="server" :request-id="page.failure.value?.requestId" :code="page.failure.value?.code" />
    <ErrorState v-else-if="page.failure.value" :failure="page.failure.value" @retry="page.reload" />
    <EmptyState v-else-if="page.status.value === 'empty'" title="No domains claimed yet" description="Claim the domain your organization sends from, publish the DNS records and verify ownership.">
      <button v-if="canManage" type="button" class="kp-button--primary" @click="openDialog">Claim domain</button>
      <p v-else class="kp-muted">Ask an organization owner or admin to claim a domain.</p>
    </EmptyState>
    <div v-else class="kp-stack">
      <div v-if="actionFailure" class="kp-notice kp-notice--danger" role="alert">
        <strong>Verification did not succeed.</strong> {{ describeFailure(actionFailure) }}
        <RequestIdentifiers :request-id="actionFailure.requestId" :correlation-id="actionFailure.correlationId" :code="actionFailure.code" :status="actionFailure.status" />
      </div>
      <SourceBadge :source="page.stale.value ? 'stale' : 'live'" />
      <DataTable caption="Domain claims" :columns="columns" :rows="page.data.value || []">
        <template #cell-domain="{ row }"><a :href="`/app/email/domains/${encodeURIComponent(String(row.id))}`">{{ row.domain }}</a></template>
        <template #cell-state="{ value }"><StatusBadge :value="String(value)" /></template>
        <template #cell-verified_at="{ value }">{{ value ? new Date(String(value)).toLocaleDateString() : 'Not verified' }}</template>
        <template #cell-created_at="{ value }">{{ value ? new Date(String(value)).toLocaleDateString() : '—' }}</template>
        <template #cell-actions="{ row }">
          <button v-if="canManage && !['VERIFIED', 'SENDING_ENABLED'].includes(String(row.state))" type="button" class="kp-button" :disabled="verifying === row.id" @click="verify(row as unknown as DomainClaim)">{{ verifying === row.id ? 'Checking DNS…' : 'Verify' }}</button>
          <span v-else class="kp-muted">—</span>
        </template>
      </DataTable>
    </div>

    <ModalDialog :open="dialogOpen" :title="created ? 'Publish these DNS records' : 'Claim a domain'" @close="closeDialog">
      <form v-if="!created" id="claim-domain-form" class="kp-form" aria-label="Claim a domain" @submit.prevent="claim">
        <FormField id="claim-domain" label="Domain" hint="Lowercase, no protocol. Subdomains are allowed." :error="formError">
          <input id="claim-domain" v-model="domainInput" type="text" autocomplete="off" required aria-describedby="claim-domain-hint claim-domain-error" :aria-invalid="formError ? 'true' : 'false'">
        </FormField>
      </form>
      <div v-else class="kp-stack">
        <p>The ownership value is shown once. Publish the records at your DNS provider, then use <strong>Verify</strong> on the domain list.</p>
        <dl class="kp-dl">
          <div><dt>Ownership TXT name</dt><dd><code class="kp-code">{{ created.result.dns.ownership.name }}</code></dd></div>
          <div><dt>Ownership TXT value</dt><dd><code class="kp-code">{{ created.result.dns.ownership.value }}</code> <CopyButton :value="created.result.dns.ownership.value" label="Copy value" /></dd></div>
          <div v-if="created.result.dns.spf"><dt>SPF</dt><dd><code class="kp-code">{{ created.result.dns.spf.recommended }}</code></dd></div>
          <div v-if="created.result.dns.dkim"><dt>DKIM selector</dt><dd><code class="kp-code">{{ created.result.dns.dkim.selector }}</code></dd></div>
        </dl>
      </div>
      <template #footer>
        <button type="button" class="kp-button" @click="closeDialog">{{ created ? 'Done' : 'Cancel' }}</button>
        <button v-if="!created" type="submit" form="claim-domain-form" class="kp-button--primary" :disabled="submitting">{{ submitting ? 'Claiming…' : 'Claim domain' }}</button>
      </template>
    </ModalDialog>
  </div>
</template>
