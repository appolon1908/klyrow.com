<script setup lang="ts">
import { computed, ref } from 'vue'
import { appApi, type BrowserSession } from '../../api'
import type { PortalRoute } from '../routes'
import type { InvitationCreated, TeamMember } from '../types'
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
import ModalDialog from '../components/ModalDialog.vue'
import FormField from '../components/FormField.vue'
import OneTimeSecret from '../components/OneTimeSecret.vue'
import PanelCard from '../components/PanelCard.vue'
import UnavailableState from '../components/UnavailableState.vue'
import SafeText from '../components/SafeText.vue'

const props = defineProps<{ route: PortalRoute; params: Record<string, string>; session: BrowserSession }>()
const canManage = computed(() => isManagementRole(props.session))
const page = usePage(() => appApi<TeamMember[]>('/app/api/team'), { isEmpty: rows => rows.length === 0 })
const columns = [{ key: 'email', label: 'Member' }, { key: 'role', label: 'Role' }, { key: 'created_at', label: 'Joined' }]
const ROLES = ['OWNER', 'ADMIN', 'DEVELOPER', 'BILLING', 'SUPPORT', 'MARKETING', 'ANALYST', 'READ_ONLY']

const dialogOpen = ref(false)
const form = ref({ email: '', role: 'READ_ONLY' })
const errors = ref<Record<string, string>>({})
const submitError = ref('')
const submitting = ref(false)
const invitation = ref<InvitationCreated | null>(null)
const pending = ref<Array<{id:string;email:string;role:string;expires_at:string;created_at:string}>>([])
async function loadPending() { if (canManage.value) pending.value = await appApi('/app/api/team/invitations') }
async function revokeInvite(id:string) { await appApi('/app/api/team/invitations/' + encodeURIComponent(id), { method: 'DELETE' }); await loadPending(); notify('success', 'Invitation revoked.') }
async function changeRole(userId:string, role:string) { await appApi('/app/api/team/' + encodeURIComponent(userId), { method:'PATCH', body: JSON.stringify({role}) }); await page.reload(); notify('success','Member role updated.') }
async function removeMember(userId:string) { await appApi('/app/api/team/' + encodeURIComponent(userId), { method:'DELETE' }); await page.reload(); notify('success','Member removed.') }
if (canManage.value) void loadPending()

function openDialog() { form.value = { email: '', role: 'READ_ONLY' }; errors.value = {}; submitError.value = ''; invitation.value = null; dialogOpen.value = true }
function closeDialog() { dialogOpen.value = false; invitation.value = null }
async function submit() {
  const next: Record<string, string> = {}
  const email = form.value.email.trim().toLowerCase()
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) next.email = 'Enter a valid email address.'
  if (!ROLES.includes(form.value.role)) next.role = 'Choose a role.'
  errors.value = next
  if (Object.keys(next).length) return
  submitting.value = true
  submitError.value = ''
  try {
    invitation.value = await appApi<InvitationCreated>('/app/api/team/invitations', { method: 'POST', body: JSON.stringify({ email, role: form.value.role, expires_hours: 72 }) })
    notify('success', `Invitation created for ${email}.`)
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
    <PageHeader title="Team" eyebrow="Settings" description="Active members of this organization and invitations. Invitation links are delivered by email; in non-production environments the one-time token is shown once here.">
      <button type="button" class="kp-button" :disabled="page.status.value === 'loading'" @click="page.reload">Refresh</button>
      <button v-if="canManage" type="button" class="kp-button--primary" @click="openDialog">Invite member</button>
    </PageHeader>
    <LoadingState v-if="page.status.value === 'loading'" label="Loading members…" />
    <ForbiddenState v-else-if="page.status.value === 'forbidden'" reason="server" :request-id="page.failure.value?.requestId" :code="page.failure.value?.code" />
    <ErrorState v-else-if="page.failure.value" :failure="page.failure.value" @retry="page.reload" />
    <div v-else class="kp-stack">
      <EmptyState v-if="page.status.value === 'empty'" title="No members listed" description="Members appear once they accept an invitation." />
      <DataTable v-else caption="Team members" :columns="columns" :rows="page.data.value || []" row-key="user_id">
        <template #cell-email="{ row }"><SafeText :value="row.email" :fallback="String(row.user_id)" /></template>
        <template #cell-role="{ row, value }">
          <select v-if="canManage" :value="String(value)" aria-label="Member role" @change="changeRole(String(row.user_id), ($event.target as HTMLSelectElement).value)">
            <option v-for="role in ROLES" :key="role" :value="role">{{ role }}</option>
          </select>
          <StatusBadge v-else :value="String(value)" />
        </template>
        <template #cell-created_at="{ row, value }">
          {{ new Date(String(value)).toLocaleDateString() }}
          <button v-if="canManage" type="button" class="kp-button" @click="removeMember(String(row.user_id))">Remove</button>
        </template>
      </DataTable>
      <PanelCard v-if="canManage" title="Pending invitations" eyebrow="Access lifecycle" source="live">
        <EmptyState v-if="pending.length === 0" title="No pending invitations" description="Outstanding invitations will appear here until accepted, revoked or expired." />
        <ul v-else>
          <li v-for="item in pending" :key="item.id">
            <SafeText :value="item.email" /> · {{ item.role }} · expires {{ new Date(item.expires_at).toLocaleString() }}
            <button type="button" class="kp-button" @click="revokeInvite(item.id)">Revoke</button>
          </li>
        </ul>
      </PanelCard>
    </div>

    <ModalDialog :open="dialogOpen" :title="invitation ? 'Invitation created' : 'Invite a member'" @close="closeDialog">
      <form v-if="!invitation" id="invite-form" class="kp-form" aria-label="Invite a member" novalidate @submit.prevent="submit">
        <p v-if="submitError" class="kp-notice kp-notice--danger" role="alert">{{ submitError }}</p>
        <FormField id="invite-email" label="Email address" :error="errors.email">
          <input id="invite-email" v-model="form.email" type="email" autocomplete="off" :aria-invalid="errors.email ? 'true' : 'false'">
        </FormField>
        <FormField id="invite-role" label="Role" hint="Roles map to server-enforced capabilities." :error="errors.role">
          <select id="invite-role" v-model="form.role" aria-describedby="invite-role-hint">
            <option v-for="role in ROLES" :key="role" :value="role">{{ role }}</option>
          </select>
        </FormField>
      </form>
      <div v-else class="kp-stack">
        <p>Invitation for <strong><SafeText :value="invitation.email" /></strong> as {{ invitation.role }}, expiring {{ new Date(invitation.expires_at).toLocaleString() }}.</p>
        <OneTimeSecret v-if="invitation.development_token" label="Development invitation token" :value="invitation.development_token" instructions="This token is only returned in non-production environments. Share it through a secure channel; it is not stored in the browser." @done="closeDialog" />
        <p v-else class="kp-muted">The invitation email carries the one-time link; nothing is displayed here.</p>
      </div>
      <template #footer>
        <button type="button" class="kp-button" @click="closeDialog">{{ invitation ? 'Close' : 'Cancel' }}</button>
        <button v-if="!invitation" type="submit" form="invite-form" class="kp-button--primary" :disabled="submitting">{{ submitting ? 'Sending…' : 'Send invitation' }}</button>
      </template>
    </ModalDialog>
  </div>
</template>
