<script setup lang="ts">
import { computed, ref } from 'vue'
import { appApi, type BrowserSession } from '../../api'
import type { PortalRoute } from '../routes'
import type { Mailbox } from '../types'
import { isManagementRole } from '../access'
import { usePage } from '../composables/usePage'
import { describeFailure, normalizeFailure } from '../errors'
import { notify } from '../toasts'
import PageHeader from '../components/PageHeader.vue'
import DataTable from '../components/DataTable.vue'
import StatusBadge from '../components/StatusBadge.vue'
import LoadingState from '../components/LoadingState.vue'
import EmptyState from '../components/EmptyState.vue'
import ErrorState from '../components/ErrorState.vue'
import ForbiddenState from '../components/ForbiddenState.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import PanelCard from '../components/PanelCard.vue'
import UnavailableState from '../components/UnavailableState.vue'
import SafeText from '../components/SafeText.vue'

const props = defineProps<{ route: PortalRoute; params: Record<string, string>; session: BrowserSession }>()
const canManage = computed(() => isManagementRole(props.session))
const page = usePage(() => appApi<Mailbox[]>('/app/api/mailboxes'), { isEmpty: rows => rows.length === 0 })
const pending = computed(() => (page.data.value || []).filter(item => !item.receiving_enabled))
const columns = [{ key: 'address', label: 'Mailbox' }, { key: 'domain', label: 'Domain' }, { key: 'sending_enabled', label: 'Sending' }, { key: 'receiving_enabled', label: 'Receiving' }, { key: 'unread', label: 'Unread' }]
const rows = computed(() => (page.data.value || []).map(item => ({ ...item, unread: item.counts?.UNREAD ?? 0 })))
const confirmOpen = ref(false)
const activating = ref(false)
const summary = ref('')

async function activate() {
  activating.value = true
  try {
    const result = await appApi<{ activated_domains: number; activated_routes: number }>('/app/api/mailboxes/inbound/activate', { method: 'POST' })
    summary.value = `Inbound activated for ${result.activated_domains} domain(s) and ${result.activated_routes} route(s).`
    notify('success', summary.value)
    confirmOpen.value = false
    await page.reload()
  } catch (error) {
    const failure = normalizeFailure(error)
    notify('error', describeFailure(failure), failure.requestId)
  } finally {
    activating.value = false
  }
}
</script>

<template>
  <div>
    <PageHeader title="Inbound" eyebrow="Email operations" description="Mailbox readiness for receiving mail. Inbound activation publishes exact routes for verified domains; it does not enable unrestricted live delivery.">
      <button type="button" class="kp-button" :disabled="page.status.value === 'loading'" @click="page.reload">Refresh</button>
      <a class="kp-button" href="/app/mail" data-external="true">Open webmail</a>
      <button v-if="canManage && pending.length" type="button" class="kp-button--primary" @click="confirmOpen = true">Activate {{ pending.length }} pending inbox{{ pending.length === 1 ? '' : 'es' }}</button>
    </PageHeader>
    <LoadingState v-if="page.status.value === 'loading'" label="Loading mailboxes…" />
    <ForbiddenState v-else-if="page.status.value === 'forbidden'" reason="server" :request-id="page.failure.value?.requestId" :code="page.failure.value?.code" />
    <ErrorState v-else-if="page.failure.value" :failure="page.failure.value" @retry="page.reload" />
    <EmptyState v-else-if="page.status.value === 'empty'" title="No mailboxes provisioned" description="Provision mailboxes for verified senders from webmail; inbound routes become available once DNS is in place.">
      <a class="kp-button--primary" href="/app/mail" data-external="true">Open webmail</a>
    </EmptyState>
    <div v-else class="kp-stack">
      <p v-if="summary" class="kp-notice kp-notice--success" role="status">{{ summary }}</p>
      <DataTable caption="Mailboxes" :columns="columns" :rows="rows">
        <template #cell-address="{ row }"><SafeText :value="row.address" /></template>
        <template #cell-sending_enabled="{ value }"><StatusBadge :value="value ? 'ready' : 'pending'" /></template>
        <template #cell-receiving_enabled="{ value }"><StatusBadge :value="value ? 'ready' : 'pending'" /></template>
      </DataTable>
      <PanelCard title="Inbound routes" eyebrow="Routing" source="unavailable">
        <UnavailableState title="Inbound route listing" dependency="No browser API exists yet. Required contract: GET /app/api/inbound/routes (route address, destination mailbox, activation state, last delivery)." />
      </PanelCard>
    </div>
    <ConfirmDialog :open="confirmOpen" title="Activate inbound mail" :description="`Publish inbound routes for ${pending.length} pending mailbox(es) on verified domains. Existing inbound destinations are kept.`" confirm-label="Activate" :busy="activating" @confirm="activate" @cancel="confirmOpen = false" />
  </div>
</template>
