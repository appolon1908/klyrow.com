<script setup lang="ts">
import { computed } from 'vue'
import { appApi, type BrowserSession } from '../../api'
import type { PortalRoute } from '../routes'
import type { MessageRow } from '../types'
import { usePage } from '../composables/usePage'
import PageHeader from '../components/PageHeader.vue'
import PanelCard from '../components/PanelCard.vue'
import StatusBadge from '../components/StatusBadge.vue'
import LoadingState from '../components/LoadingState.vue'
import EmptyState from '../components/EmptyState.vue'
import ErrorState from '../components/ErrorState.vue'
import ForbiddenState from '../components/ForbiddenState.vue'
import UnavailableState from '../components/UnavailableState.vue'
import ActivityTimeline from '../components/ActivityTimeline.vue'
import SafeText from '../components/SafeText.vue'

const props = defineProps<{ route: PortalRoute; params: Record<string, string>; session: BrowserSession }>()
// There is no per-message browser endpoint; the summary is located in the message list.
const page = usePage(() => appApi<MessageRow[]>('/app/api/messages?limit=200&offset=0'))
const message = computed(() => (page.data.value || []).find(row => row.id === props.params.id) || null)
const timeline = computed(() => message.value ? [{ id: 'accepted', title: 'Accepted', detail: `Recorded with status ${message.value.status}`, at: message.value.created_at, tone: 'success' as const }] : [])
</script>

<template>
  <div>
    <PageHeader :title="message ? (message.subject || '(no subject)') : 'Message'" eyebrow="Message detail">
      <a class="kp-button" href="/app/email/messages">Back to messages</a>
    </PageHeader>
    <LoadingState v-if="page.status.value === 'loading'" label="Loading message…" />
    <ForbiddenState v-else-if="page.status.value === 'forbidden'" reason="server" :request-id="page.failure.value?.requestId" :code="page.failure.value?.code" />
    <ErrorState v-else-if="page.failure.value" :failure="page.failure.value" @retry="page.reload" />
    <EmptyState v-else-if="!message" title="Message not found" description="The message is not in the most recent 200 records for this organization, or it belongs to another organization." />
    <div v-else class="kp-grid--2">
      <PanelCard title="Summary" eyebrow="Recorded intent" source="live">
        <dl class="kp-dl">
          <div><dt>Message ID</dt><dd><code class="kp-code">{{ message.id }}</code></dd></div>
          <div><dt>Recipient</dt><dd><SafeText :value="message.recipient" /></dd></div>
          <div><dt>Sender</dt><dd><SafeText :value="message.sender" /></dd></div>
          <div><dt>Subject</dt><dd><SafeText :value="message.subject" fallback="(no subject)" /></dd></div>
          <div><dt>Status</dt><dd><StatusBadge :value="message.status" /></dd></div>
          <div><dt>Created</dt><dd><time :datetime="message.created_at">{{ new Date(message.created_at).toLocaleString() }}</time></dd></div>
        </dl>
        <p class="kp-muted">Use the message ID as the correlation reference when contacting support.</p>
      </PanelCard>
      <PanelCard title="Event timeline" eyebrow="Delivery events" source="unavailable">
        <ActivityTimeline :entries="timeline" />
        <UnavailableState title="Provider event timeline" dependency="No browser API exists yet. Required contract: GET /app/api/messages/{id} returning accepted → queued → provider → delivered/bounced/complained events with request and correlation identifiers, plus retry and cancel actions." />
      </PanelCard>
    </div>
  </div>
</template>
