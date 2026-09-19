<script setup lang="ts">
import { computed, ref } from 'vue'
import { appApi, type BrowserSession } from '../../api'
import type { PortalRoute } from '../routes'
import type { BrowserSessionRow } from '../types'
import { usePage } from '../composables/usePage'
import { describeFailure, normalizeFailure } from '../errors'
import { notify } from '../toasts'
import PageHeader from '../components/PageHeader.vue'
import DataTable from '../components/DataTable.vue'
import StatusBadge from '../components/StatusBadge.vue'
import LoadingState from '../components/LoadingState.vue'
import ErrorState from '../components/ErrorState.vue'
import ForbiddenState from '../components/ForbiddenState.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import PanelCard from '../components/PanelCard.vue'

defineProps<{ route: PortalRoute; params: Record<string, string>; session: BrowserSession }>()
const page = usePage(() => appApi<BrowserSessionRow[]>('/auth/sessions'))
const columns = [{ key: 'device', label: 'Session' }, { key: 'created_at', label: 'Signed in' }, { key: 'last_seen_at', label: 'Last seen' }, { key: 'expires_at', label: 'Expires' }, { key: 'state', label: 'State' }, { key: 'actions', label: 'Actions' }]
const rows = computed(() => (page.data.value || []).map(row => ({
  ...row,
  device: row.current ? 'This device' : `Device ${String(row.user_agent_hash || row.id).slice(0, 12)}`,
  state: row.revoked_at ? 'revoked' : Date.parse(row.expires_at) < Date.now() ? 'expired' : 'active',
})))
const revokeTarget = ref<BrowserSessionRow | null>(null)
const logoutAllOpen = ref(false)
const busy = ref(false)

async function revoke() {
  if (!revokeTarget.value) return
  busy.value = true
  try {
    await appApi(`/auth/sessions/${encodeURIComponent(revokeTarget.value.id)}`, { method: 'DELETE' })
    notify('success', 'Session revoked.')
    revokeTarget.value = null
    await page.reload()
  } catch (error) {
    const failure = normalizeFailure(error)
    notify('error', describeFailure(failure), failure.requestId)
  } finally {
    busy.value = false
  }
}
async function logoutAll() {
  busy.value = true
  try {
    await appApi('/auth/logout-all', { method: 'POST' })
    location.assign('/logged-out')
  } catch (error) {
    const failure = normalizeFailure(error)
    notify('error', describeFailure(failure), failure.requestId)
    busy.value = false
  }
}
</script>

<template>
  <div>
    <PageHeader title="Security" eyebrow="Settings" description="Browser sessions on your account. Identifiers are hashed by the server; no token or cookie value is ever displayed.">
      <button type="button" class="kp-button" :disabled="page.status.value === 'loading'" @click="page.reload">Refresh</button>
      <button type="button" class="kp-button--danger" @click="logoutAllOpen = true">Sign out everywhere</button>
    </PageHeader>
    <LoadingState v-if="page.status.value === 'loading'" label="Loading sessions…" />
    <ForbiddenState v-else-if="page.status.value === 'forbidden'" reason="server" :request-id="page.failure.value?.requestId" :code="page.failure.value?.code" />
    <ErrorState v-else-if="page.failure.value" :failure="page.failure.value" @retry="page.reload" />
    <div v-else class="kp-stack">
      <DataTable caption="Browser sessions" :columns="columns" :rows="rows">
        <template #cell-device="{ row }">{{ row.device }} <span v-if="row.user_agent_hash" class="kp-muted">({{ String(row.user_agent_hash).slice(0, 8) }})</span></template>
        <template #cell-created_at="{ value }">{{ new Date(String(value)).toLocaleString() }}</template>
        <template #cell-last_seen_at="{ value }">{{ value ? new Date(String(value)).toLocaleString() : '—' }}</template>
        <template #cell-expires_at="{ value }">{{ new Date(String(value)).toLocaleString() }}</template>
        <template #cell-state="{ value }"><StatusBadge :value="String(value)" /></template>
        <template #cell-actions="{ row }">
          <button v-if="!row.current && !row.revoked_at" type="button" class="kp-button--danger" @click="revokeTarget = row as unknown as BrowserSessionRow">Revoke</button>
          <span v-else class="kp-muted">—</span>
        </template>
      </DataTable>
      <PanelCard title="MFA and step-up" eyebrow="Identity provider">
        <p>Multi-factor authentication and fresh-authentication step-up are enforced by Keycloak and verified by the server on protected operations. Enrolment happens in your identity provider account, not in this portal.</p>
      </PanelCard>
    </div>
    <ConfirmDialog :open="!!revokeTarget" title="Revoke session" description="The device will be signed out on its next request. This cannot be undone." confirm-label="Revoke session" destructive :busy="busy" @confirm="revoke" @cancel="revokeTarget = null" />
    <ConfirmDialog :open="logoutAllOpen" title="Sign out everywhere" description="Every browser session on your account, including this one, will be revoked." confirm-label="Sign out everywhere" destructive :busy="busy" @confirm="logoutAll" @cancel="logoutAllOpen = false" />
  </div>
</template>
