<script setup lang="ts">
import { computed } from 'vue'
import { appApi, type BrowserSession } from '../../api'
import type { PortalRoute } from '../routes'
import type { MessageDetail } from '../types'
import { usePage } from '../composables/usePage'
import PageHeader from '../components/PageHeader.vue'
import PanelCard from '../components/PanelCard.vue'
import StatusBadge from '../components/StatusBadge.vue'
import LoadingState from '../components/LoadingState.vue'
import EmptyState from '../components/EmptyState.vue'
import ErrorState from '../components/ErrorState.vue'
import ForbiddenState from '../components/ForbiddenState.vue'
import ActivityTimeline from '../components/ActivityTimeline.vue'
import SafeText from '../components/SafeText.vue'

const props = defineProps<{ route: PortalRoute; params: Record<string, string>; session: BrowserSession }>()
const page = usePage(() => appApi<MessageDetail>(`/app/api/messages/${encodeURIComponent(props.params.id)}`))
const message = computed(() => page.data.value)
const timeline = computed(() => (message.value?.timeline || []).map(entry => ({
  id: entry.id,
  title: entry.status.replace(/_/g, ' ').toLowerCase(),
  detail: `${entry.kind} · ${entry.source.replace(/_/g, ' ')}`,
  at: entry.occurred_at,
  tone: ['DELIVERED', 'SENT', 'SUBMITTED'].includes(entry.status)
    ? 'success' as const
    : ['FAILED', 'BOUNCED_HARD', 'BOUNCED_SOFT', 'COMPLAINED', 'DEAD_LETTER'].includes(entry.status)
      ? 'danger' as const
      : ['DEFERRED', 'INDETERMINATE', 'SUPPRESSED'].includes(entry.status)
        ? 'warning' as const
        : '' as const,
})))
</script>

<template>
  <div>
    <PageHeader :title="message ? (message.subject || '(no subject)') : 'Message'" eyebrow="Message detail">
      <a class="kp-button" href="/app/email/messages">Back to messages</a>
    </PageHeader>
    <LoadingState v-if="page.status.value === 'loading'" label="Loading message evidence…" />
    <ForbiddenState v-else-if="page.status.value === 'forbidden'" reason="server" :request-id="page.failure.value?.requestId" :code="page.failure.value?.code" />
    <ErrorState v-else-if="page.failure.value" :failure="page.failure.value" @retry="page.reload" />
    <EmptyState v-else-if="!message" title="Message not found" description="This message does not exist in the current organization." />
    <div v-else class="kp-grid--2">
      <PanelCard title="Summary" eyebrow="Recorded intent" source="live">
        <dl class="kp-dl">
          <div><dt>Message ID</dt><dd><code class="kp-code">{{ message.id }}</code></dd></div>
          <div><dt>Recipient</dt><dd><SafeText :value="message.recipient" /></dd></div>
          <div><dt>Sender</dt><dd><SafeText :value="message.sender" /></dd></div>
          <div><dt>Subject</dt><dd><SafeText :value="message.subject" fallback="(no subject)" /></dd></div>
          <div><dt>Recorded status</dt><dd><StatusBadge :value="message.status" /></dd></div>
          <div><dt>Current outcome</dt><dd><StatusBadge :value="message.current_outcome" /></dd></div>
          <div><dt>Created</dt><dd><time :datetime="message.created_at">{{ new Date(message.created_at).toLocaleString() }}</time></dd></div>
          <div v-if="message.correlation_id"><dt>Correlation ID</dt><dd><code class="kp-code">{{ message.correlation_id }}</code></dd></div>
          <div v-if="message.operation_id"><dt>Operation ID</dt><dd><code class="kp-code">{{ message.operation_id }}</code></dd></div>
        </dl>
      </PanelCard>
      <PanelCard title="Delivery evidence" eyebrow="Accepted → provider outcome" source="live">
        <ActivityTimeline :entries="timeline" empty-message="No delivery evidence has been recorded beyond the message record." />
        <p class="kp-muted">Timeline entries are normalized evidence. Raw provider callback payloads and credentials are never exposed here.</p>
      </PanelCard>
      <PanelCard title="Outbox" eyebrow="Durable execution" :source="message.outbox ? 'live' : 'unavailable'">
        <dl v-if="message.outbox" class="kp-dl">
          <div><dt>State</dt><dd><StatusBadge :value="message.outbox.state" /></dd></div>
          <div><dt>Attempts</dt><dd>{{ message.outbox.attempts }}</dd></div>
          <div><dt>Updated</dt><dd>{{ new Date(message.outbox.updated_at).toLocaleString() }}</dd></div>
          <div><dt>Provider reference</dt><dd>{{ message.outbox.provider_reference_present ? 'Recorded' : 'Not recorded' }}</dd></div>
        </dl>
        <p v-else class="kp-muted">No durable outbox record is associated with this message.</p>
      </PanelCard>
      <PanelCard title="Provider projection" eyebrow="Read-only evidence" :source="message.provider ? 'live' : 'unavailable'">
        <dl v-if="message.provider" class="kp-dl">
          <div><dt>Status</dt><dd><StatusBadge :value="message.provider.status" /></dd></div>
          <div><dt>Attempts</dt><dd>{{ message.provider.attempts }}</dd></div>
          <div><dt>Mode</dt><dd>{{ message.provider.sandbox ? 'Sandbox' : 'Provider runtime' }}</dd></div>
          <div><dt>Updated</dt><dd>{{ new Date(message.provider.updated_at).toLocaleString() }}</dd></div>
        </dl>
        <p v-else class="kp-muted">No provider projection is available yet. Unknown is not treated as delivered.</p>
      </PanelCard>
    </div>
  </div>
</template>
