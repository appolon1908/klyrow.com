<script setup lang="ts">
import { computed, ref } from 'vue'
import { appApi, idempotencyKey, type BrowserSession } from '../../api'
import type { PortalRoute } from '../routes'
import type { SupportTicketDetail, SupportTicketPage } from '../types'
import { usePage } from '../composables/usePage'
import { describeFailure, normalizeFailure } from '../errors'
import { notify } from '../toasts'
import PageHeader from '../components/PageHeader.vue'
import PanelCard from '../components/PanelCard.vue'
import DataTable from '../components/DataTable.vue'
import StatusBadge from '../components/StatusBadge.vue'
import LoadingState from '../components/LoadingState.vue'
import EmptyState from '../components/EmptyState.vue'
import ErrorState from '../components/ErrorState.vue'
import ForbiddenState from '../components/ForbiddenState.vue'
import ModalDialog from '../components/ModalDialog.vue'
import FormField from '../components/FormField.vue'
import SafeText from '../components/SafeText.vue'

const props = defineProps<{ route: PortalRoute; params: Record<string, string>; session: BrowserSession }>()
const isDetail = computed(() => props.route.name === 'support-ticket')
const ticketId = computed(() => props.params.id || '')
type SupportData = SupportTicketPage | SupportTicketDetail
const page = usePage<SupportData>(
  () => isDetail.value
    ? appApi<SupportTicketDetail>('/app/api/support/tickets/' + encodeURIComponent(ticketId.value))
    : appApi<SupportTicketPage>('/app/api/support/tickets?offset=0&limit=50'),
  { isEmpty: data => !isDetail.value && (data as SupportTicketPage).items.length === 0 },
)
const list = computed(() => isDetail.value ? [] : ((page.data.value as SupportTicketPage | null)?.items || []))
const detail = computed(() => isDetail.value ? (page.data.value as SupportTicketDetail | null) : null)
const columns = [
  { key: 'subject', label: 'Subject' },
  { key: 'category', label: 'Category' },
  { key: 'priority', label: 'Priority' },
  { key: 'status', label: 'Status' },
  { key: 'last_message_at', label: 'Updated' },
]

const createOpen = ref(false)
const createForm = ref({ subject: '', category: 'technical', priority: 'NORMAL', body: '' })
const createError = ref('')
const creating = ref(false)
const replyBody = ref('')
const replyError = ref('')
const replying = ref(false)

function openCreate() {
  createForm.value = { subject: '', category: 'technical', priority: 'NORMAL', body: '' }
  createError.value = ''
  createOpen.value = true
}

async function createTicket() {
  const subject = createForm.value.subject.trim()
  const body = createForm.value.body.trim()
  if (subject.length < 3 || !body) {
    createError.value = 'Enter a subject and a description.'
    return
  }
  creating.value = true
  createError.value = ''
  try {
    const created = await appApi<SupportTicketDetail>('/app/api/support/tickets', {
      method: 'POST',
      headers: { 'Idempotency-Key': idempotencyKey('support-ticket') },
      body: JSON.stringify({ ...createForm.value, subject, body }),
    })
    notify('success', 'Support ticket created.')
    location.assign('/app/support/tickets/' + encodeURIComponent(created.id))
  } catch (error) {
    const failure = normalizeFailure(error)
    createError.value = describeFailure(failure)
  } finally {
    creating.value = false
  }
}

async function reply() {
  const body = replyBody.value.trim()
  if (!body) {
    replyError.value = 'Enter a reply.'
    return
  }
  replying.value = true
  replyError.value = ''
  try {
    await appApi<SupportTicketDetail>('/app/api/support/tickets/' + encodeURIComponent(ticketId.value) + '/messages', {
      method: 'POST',
      headers: { 'Idempotency-Key': idempotencyKey('support-reply') },
      body: JSON.stringify({ body }),
    })
    replyBody.value = ''
    notify('success', 'Reply added.')
    await page.reload()
  } catch (error) {
    const failure = normalizeFailure(error)
    replyError.value = describeFailure(failure)
  } finally {
    replying.value = false
  }
}

function ticketHref(row: Record<string, unknown>) {
  return '/app/support/tickets/' + encodeURIComponent(String(row.id ?? ''))
}
</script>

<template>
  <div>
    <PageHeader
      :title="isDetail ? (detail?.subject || 'Support ticket') : 'Support'"
      eyebrow="Support"
      :description="isDetail ? 'Tenant-scoped support conversation.' : 'Create and track support requests for this organization.'"
    >
      <a v-if="isDetail" class="kp-button" href="/app/support">All tickets</a>
      <button type="button" class="kp-button" :disabled="page.status.value === 'loading'" @click="page.reload">Refresh</button>
      <button v-if="!isDetail" type="button" class="kp-button--primary" @click="openCreate">New ticket</button>
    </PageHeader>

    <p class="kp-notice">Support requests are stored inside Klyrow. Creating or replying to a ticket does not send email, SMS, or trigger an external provider.</p>

    <LoadingState v-if="page.status.value === 'loading'" label="Loading support…" />
    <ForbiddenState v-else-if="page.status.value === 'forbidden'" reason="server" :request-id="page.failure.value?.requestId" :code="page.failure.value?.code" />
    <ErrorState v-else-if="page.failure.value" :failure="page.failure.value" @retry="page.reload" />

    <template v-else-if="!isDetail">
      <EmptyState v-if="page.status.value === 'empty'" title="No support tickets" description="Create a ticket when you need help from the Klyrow team." />
      <DataTable v-else caption="Support tickets" :columns="columns" :rows="list" row-key="id">
        <template #cell-subject="{ row }"><a :href="ticketHref(row)"><SafeText :value="row.subject" /></a></template>
        <template #cell-category="{ value }"><StatusBadge :value="String(value)" /></template>
        <template #cell-priority="{ value }"><StatusBadge :value="String(value)" /></template>
        <template #cell-status="{ value }"><StatusBadge :value="String(value)" /></template>
        <template #cell-last_message_at="{ value }">{{ new Date(String(value)).toLocaleString() }}</template>
      </DataTable>
    </template>

    <div v-else-if="detail" class="kp-stack">
      <PanelCard title="Ticket" eyebrow="Current status" source="live">
        <dl class="kp-definition-list">
          <div><dt>Status</dt><dd><StatusBadge :value="detail.status" /></dd></div>
          <div><dt>Category</dt><dd>{{ detail.category }}</dd></div>
          <div><dt>Priority</dt><dd>{{ detail.priority }}</dd></div>
          <div><dt>Created</dt><dd>{{ new Date(detail.created_at).toLocaleString() }}</dd></div>
        </dl>
      </PanelCard>

      <PanelCard title="Conversation" eyebrow="Tenant-scoped" source="live">
        <ol class="kp-list">
          <li v-for="message in detail.messages" :key="message.id">
            <strong>{{ message.author_kind === 'CUSTOMER' ? 'You' : message.author_kind }}</strong>
            · {{ new Date(message.created_at).toLocaleString() }}
            <p><SafeText :value="message.body" /></p>
          </li>
        </ol>
      </PanelCard>

      <PanelCard v-if="detail.status !== 'CLOSED'" title="Add reply" eyebrow="Customer reply" source="live">
        <form class="kp-form" @submit.prevent="reply">
          <p v-if="replyError" class="kp-notice kp-notice--danger" role="alert">{{ replyError }}</p>
          <FormField id="support-reply" label="Reply">
            <textarea id="support-reply" v-model="replyBody" rows="6" maxlength="5000" />
          </FormField>
          <button type="submit" class="kp-button--primary" :disabled="replying">{{ replying ? 'Adding…' : 'Add reply' }}</button>
        </form>
      </PanelCard>
    </div>

    <ModalDialog :open="createOpen" title="New support ticket" @close="createOpen = false">
      <form id="support-create-form" class="kp-form" @submit.prevent="createTicket">
        <p v-if="createError" class="kp-notice kp-notice--danger" role="alert">{{ createError }}</p>
        <FormField id="support-subject" label="Subject">
          <input id="support-subject" v-model="createForm.subject" maxlength="160">
        </FormField>
        <FormField id="support-category" label="Category">
          <select id="support-category" v-model="createForm.category">
            <option value="technical">Technical</option>
            <option value="deliverability">Deliverability</option>
            <option value="billing">Billing</option>
            <option value="account">Account</option>
            <option value="abuse">Abuse</option>
            <option value="domain">Domain</option>
          </select>
        </FormField>
        <FormField id="support-priority" label="Priority">
          <select id="support-priority" v-model="createForm.priority">
            <option value="LOW">Low</option>
            <option value="NORMAL">Normal</option>
            <option value="HIGH">High</option>
          </select>
        </FormField>
        <FormField id="support-description" label="Description">
          <textarea id="support-description" v-model="createForm.body" rows="8" maxlength="5000" />
        </FormField>
      </form>
      <template #footer>
        <button type="button" class="kp-button" @click="createOpen = false">Cancel</button>
        <button type="submit" form="support-create-form" class="kp-button--primary" :disabled="creating">{{ creating ? 'Creating…' : 'Create ticket' }}</button>
      </template>
    </ModalDialog>
  </div>
</template>
