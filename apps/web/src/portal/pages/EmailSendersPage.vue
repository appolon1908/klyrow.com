<script setup lang="ts">
import { computed, ref } from 'vue'
import { appApi, type BrowserSession } from '../../api'
import type { PortalRoute } from '../routes'
import type { DomainClaim, SenderIdentity } from '../types'
import { isManagementRole } from '../access'
import { settle } from '../composables/usePage'
import { describeFailure, normalizeFailure, type PortalFailure } from '../errors'
import { notify } from '../toasts'
import PageHeader from '../components/PageHeader.vue'
import DataTable from '../components/DataTable.vue'
import StatusBadge from '../components/StatusBadge.vue'
import LoadingState from '../components/LoadingState.vue'
import EmptyState from '../components/EmptyState.vue'
import ErrorState from '../components/ErrorState.vue'
import ForbiddenState from '../components/ForbiddenState.vue'
import DegradedState from '../components/DegradedState.vue'
import ModalDialog from '../components/ModalDialog.vue'
import FormField from '../components/FormField.vue'
import SafeText from '../components/SafeText.vue'

const props = defineProps<{ route: PortalRoute; params: Record<string, string>; session: BrowserSession }>()
const canManage = computed(() => isManagementRole(props.session))
const loading = ref(true)
const senders = ref<SenderIdentity[]>([])
const domains = ref<DomainClaim[]>([])
const sendersFailure = ref<PortalFailure | null>(null)
const domainsFailure = ref<PortalFailure | null>(null)
const verifiedDomains = computed(() => domains.value.filter(item => ['VERIFIED', 'SENDING_ENABLED'].includes(item.state)))
const columns = [{ key: 'address', label: 'Address' }, { key: 'display_name', label: 'Display name' }, { key: 'domain', label: 'Domain' }, { key: 'stream', label: 'Stream' }, { key: 'status', label: 'Status' }, { key: 'verified', label: 'Verified' }]
const rows = computed(() => senders.value.map(item => ({ ...item, domain: domains.value.find(claim => claim.id === item.domain_claim_id)?.domain || item.address.split('@')[1] || '' })))

async function load() {
  loading.value = true
  const result = await settle({ senders: () => appApi<SenderIdentity[]>('/app/api/senders'), domains: () => appApi<DomainClaim[]>('/app/api/domains') })
  senders.value = result.senders.value || []
  domains.value = result.domains.value || []
  sendersFailure.value = result.senders.failure
  domainsFailure.value = result.domains.failure
  loading.value = false
}
void load()

const dialogOpen = ref(false)
const form = ref({ domain_claim_id: '', email: '', display_name: '', reply_to: '', stream: 'TRANSACTIONAL' })
const errors = ref<Record<string, string>>({})
const submitting = ref(false)
const submitError = ref('')
function openDialog() {
  form.value = { domain_claim_id: verifiedDomains.value[0]?.id || '', email: '', display_name: '', reply_to: '', stream: 'TRANSACTIONAL' }
  errors.value = {}
  submitError.value = ''
  dialogOpen.value = true
}
function validate(): boolean {
  const next: Record<string, string> = {}
  const claim = verifiedDomains.value.find(item => item.id === form.value.domain_claim_id)
  if (!claim) next.domain_claim_id = 'Choose a verified domain.'
  const email = form.value.email.trim().toLowerCase()
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) next.email = 'Enter a valid email address.'
  else if (claim && email.split('@')[1] !== claim.domain) next.email = `The address must end with @${claim.domain}.`
  if (!form.value.display_name.trim()) next.display_name = 'Enter a display name.'
  if (form.value.reply_to && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(form.value.reply_to.trim())) next.reply_to = 'Enter a valid reply-to address or leave it empty.'
  errors.value = next
  return Object.keys(next).length === 0
}
async function submit() {
  if (!validate()) return
  submitting.value = true
  submitError.value = ''
  try {
    await appApi('/app/api/senders', { method: 'POST', body: JSON.stringify({
      domain_claim_id: form.value.domain_claim_id, email: form.value.email.trim().toLowerCase(), display_name: form.value.display_name.trim(),
      reply_to: form.value.reply_to.trim().toLowerCase() || null, stream: form.value.stream,
    }) })
    notify('success', 'Sender identity created.')
    dialogOpen.value = false
    await load()
  } catch (error) {
    const failure = normalizeFailure(error)
    submitError.value = `${describeFailure(failure)}${failure.requestId ? ` (request ${failure.requestId})` : ''}`
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div>
    <PageHeader title="Senders" eyebrow="Email operations" description="Sender identities are bound to verified domains. Klyrow refuses addresses on unverified domains and never stores provider credentials with a sender.">
      <button type="button" class="kp-button" :disabled="loading" @click="load">Refresh</button>
      <button v-if="canManage" type="button" class="kp-button--primary" :disabled="loading || !verifiedDomains.length" :title="verifiedDomains.length ? '' : 'Verify a domain first'" @click="openDialog">Add sender</button>
    </PageHeader>
    <LoadingState v-if="loading" label="Loading senders…" />
    <ForbiddenState v-else-if="sendersFailure?.kind === 'forbidden'" reason="server" :request-id="sendersFailure.requestId" :code="sendersFailure.code" />
    <ErrorState v-else-if="sendersFailure" :failure="sendersFailure" @retry="load" />
    <div v-else class="kp-stack">
      <DegradedState v-if="domainsFailure" message="Domain claims could not be read; domain names are derived from addresses." :request-id="domainsFailure.requestId" :code="domainsFailure.code" />
      <div v-if="canManage && !verifiedDomains.length && !domainsFailure" class="kp-notice kp-notice--warning" role="status">No verified domain yet. <a href="/app/email/domains">Verify a domain</a> before adding senders.</div>
      <EmptyState v-if="!senders.length" title="No senders yet" description="Add a sender identity on a verified domain to send from this organization.">
        <button v-if="canManage" type="button" class="kp-button--primary" :disabled="!verifiedDomains.length" @click="openDialog">Add sender</button>
      </EmptyState>
      <DataTable v-else caption="Sender identities" :columns="columns" :rows="rows">
        <template #cell-status="{ value }"><StatusBadge :value="String(value)" /></template>
        <template #cell-verified="{ value }"><StatusBadge :value="Boolean(value)" /></template>
        <template #cell-display_name="{ value }"><SafeText :value="value" /></template>
      </DataTable>
      <p class="kp-muted">Suspension and approval actions have no browser API yet; contact platform operations to change a sender state.</p>
    </div>

    <ModalDialog :open="dialogOpen" title="Add sender" @close="dialogOpen = false">
      <form id="sender-form" class="kp-form" aria-label="Add sender" novalidate @submit.prevent="submit">
        <p v-if="submitError" class="kp-notice kp-notice--danger" role="alert">{{ submitError }}</p>
        <FormField id="sender-domain" label="Verified domain" :error="errors.domain_claim_id">
          <select id="sender-domain" v-model="form.domain_claim_id" :aria-invalid="errors.domain_claim_id ? 'true' : 'false'">
            <option v-for="item in verifiedDomains" :key="item.id" :value="item.id">{{ item.domain }}</option>
          </select>
        </FormField>
        <FormField id="sender-email" label="Email address" hint="Must belong to the selected domain." :error="errors.email">
          <input id="sender-email" v-model="form.email" type="email" autocomplete="off" aria-describedby="sender-email-hint sender-email-error" :aria-invalid="errors.email ? 'true' : 'false'">
        </FormField>
        <FormField id="sender-name" label="Display name" :error="errors.display_name">
          <input id="sender-name" v-model="form.display_name" type="text" maxlength="150" :aria-invalid="errors.display_name ? 'true' : 'false'">
        </FormField>
        <FormField id="sender-reply" label="Reply-to (optional)" :error="errors.reply_to">
          <input id="sender-reply" v-model="form.reply_to" type="email" autocomplete="off" :aria-invalid="errors.reply_to ? 'true' : 'false'">
        </FormField>
        <FormField id="sender-stream" label="Stream">
          <select id="sender-stream" v-model="form.stream">
            <option value="TRANSACTIONAL">Transactional</option>
            <option value="MARKETING">Marketing</option>
            <option value="SECURITY">Security</option>
          </select>
        </FormField>
      </form>
      <template #footer>
        <button type="button" class="kp-button" @click="dialogOpen = false">Cancel</button>
        <button type="submit" form="sender-form" class="kp-button--primary" :disabled="submitting">{{ submitting ? 'Saving…' : 'Create sender' }}</button>
      </template>
    </ModalDialog>
  </div>
</template>
