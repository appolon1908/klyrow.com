<script setup lang="ts">
import { computed, ref } from 'vue'
import { appApi, type BrowserSession } from '../../api'
import type { PortalRoute } from '../routes'
import { usePage } from '../composables/usePage'
import { describeFailure, normalizeFailure } from '../errors'
import { notify } from '../toasts'
import PageHeader from '../components/PageHeader.vue'
import DataTable from '../components/DataTable.vue'
import LoadingState from '../components/LoadingState.vue'
import EmptyState from '../components/EmptyState.vue'
import ErrorState from '../components/ErrorState.vue'
import ForbiddenState from '../components/ForbiddenState.vue'
import ModalDialog from '../components/ModalDialog.vue'
import FormField from '../components/FormField.vue'
import SafeText from '../components/SafeText.vue'

type Suppression = { id: string; email: string; reason: string }
type SuppressionResponse = { items: Suppression[]; limit: number; offset: number; has_more: boolean }

const props = defineProps<{ route: PortalRoute; params: Record<string, string>; session: BrowserSession }>()
const page = usePage(() => appApi<SuppressionResponse>('/app/api/suppressions?limit=100&offset=0'), { isEmpty: result => result.items.length === 0 })
const rows = computed(() => page.data.value?.items || [])
const dialogOpen = ref(false)
const email = ref('')
const reason = ref('manual')
const submitError = ref('')
const submitting = ref(false)
const columns = [{ key: 'email', label: 'Email' }, { key: 'reason', label: 'Reason' }, { key: 'actions', label: 'Actions' }]

async function add() {
  submitting.value = true
  submitError.value = ''
  try {
    await appApi('/app/api/suppressions', { method: 'POST', headers: { 'Idempotency-Key': crypto.randomUUID() }, body: JSON.stringify({ email: email.value.trim(), reason: reason.value.trim() }) })
    dialogOpen.value = false
    email.value = ''
    notify('success', 'Suppression saved.')
    await page.reload()
  } catch (error) {
    const failure = normalizeFailure(error)
    submitError.value = `${describeFailure(failure)}${failure.requestId ? ` (request ${failure.requestId})` : ''}`
  } finally {
    submitting.value = false
  }
}

async function remove(item: Suppression) {
  if (!window.confirm(`Remove suppression for ${item.email}?`)) return
  try {
    await appApi(`/app/api/suppressions/${encodeURIComponent(item.id)}`, { method: 'DELETE' })
    notify('success', 'Suppression removed.')
    await page.reload()
  } catch (error) {
    const failure = normalizeFailure(error)
    submitError.value = `${describeFailure(failure)}${failure.requestId ? ` (request ${failure.requestId})` : ''}`
  }
}
</script>

<template>
  <div>
    <PageHeader title="Suppressions" eyebrow="Email operations" description="Tenant-scoped delivery blocks. Adding or removing a suppression changes recipient eligibility.">
      <button type="button" class="kp-button" :disabled="page.status.value === 'loading'" @click="page.reload">Refresh</button>
      <button type="button" class="kp-button--primary" @click="dialogOpen = true">Add suppression</button>
    </PageHeader>
    <LoadingState v-if="page.status.value === 'loading'" label="Loading suppressions…" />
    <ForbiddenState v-else-if="page.status.value === 'forbidden'" reason="server" :request-id="page.failure.value?.requestId" :code="page.failure.value?.code" />
    <ErrorState v-else-if="page.failure.value" :failure="page.failure.value" @retry="page.reload" />
    <EmptyState v-else-if="page.status.value === 'empty'" title="No suppressions" description="No tenant recipients are currently blocked.">
      <button type="button" class="kp-button--primary" @click="dialogOpen = true">Add suppression</button>
    </EmptyState>
    <DataTable v-else caption="Suppressions" :columns="columns" :rows="rows" row-key="id">
      <template #cell-email="{ value }"><SafeText :value="value" /></template>
      <template #cell-actions="{ row }"><button type="button" class="kp-button" @click="remove(row as Suppression)">Remove</button></template>
    </DataTable>
    <p v-if="submitError" class="kp-notice kp-notice--danger" role="alert">{{ submitError }}</p>
    <ModalDialog :open="dialogOpen" title="Add suppression" @close="dialogOpen = false">
      <form id="suppression-form" class="kp-form" @submit.prevent="add">
        <FormField id="suppression-email" label="Email address"><input id="suppression-email" v-model="email" type="email" required autocomplete="off"></FormField>
        <FormField id="suppression-reason" label="Reason"><input id="suppression-reason" v-model="reason" type="text" maxlength="100" required></FormField>
      </form>
      <template #footer>
        <button type="button" class="kp-button" @click="dialogOpen = false">Cancel</button>
        <button type="submit" form="suppression-form" class="kp-button--primary" :disabled="submitting">{{ submitting ? 'Saving…' : 'Add suppression' }}</button>
      </template>
    </ModalDialog>
  </div>
</template>
