<script setup lang="ts">
import { computed } from 'vue'
import { appApi, type BrowserSession } from '../../api'
import type { PortalRoute } from '../routes'
import type { MessageRow } from '../types'
import { usePage } from '../composables/usePage'
import PageHeader from '../components/PageHeader.vue'
import DataTable from '../components/DataTable.vue'
import StatusBadge from '../components/StatusBadge.vue'
import PanelCard from '../components/PanelCard.vue'
import LoadingState from '../components/LoadingState.vue'
import EmptyState from '../components/EmptyState.vue'
import ErrorState from '../components/ErrorState.vue'
import ForbiddenState from '../components/ForbiddenState.vue'
import UnavailableState from '../components/UnavailableState.vue'
import SafeText from '../components/SafeText.vue'

defineProps<{ route: PortalRoute; params: Record<string, string>; session: BrowserSession }>()
const page = usePage(() => appApi<MessageRow[]>('/app/api/messages?limit=100&offset=0'), { isEmpty: rows => rows.length === 0 })
const columns = [{ key: 'id', label: 'Message ID' }, { key: 'status', label: 'Status' }, { key: 'recipient', label: 'Recipient' }, { key: 'created_at', label: 'Accepted at' }]
const rows = computed(() => page.data.value || [])
// Copy-safe example: the credential placeholder is a literal string, never a real key.
const example = 'curl -X POST https://app.klyrow.com/v1/messages \\\n  -H \'Authorization: Bearer <redacted>\' \\\n  -H \'Idempotency-Key: <unique-key>\' \\\n  -H \'Content-Type: application/json\' \\\n  -d \'{"to":"recipient@example.com","sender":"hello@your-domain","subject":"Hello","text":"Body"}\''
</script>

<template>
  <div>
    <PageHeader title="Logs" eyebrow="Developer" description="Accepted message intents with their identifiers, read from the message API. An operation log (API calls, webhook attempts) has no browser API yet.">
      <button type="button" class="kp-button" :disabled="page.status.value === 'loading'" @click="page.reload">Refresh</button>
    </PageHeader>
    <LoadingState v-if="page.status.value === 'loading'" label="Loading recent intents…" />
    <ForbiddenState v-else-if="page.status.value === 'forbidden'" reason="server" :request-id="page.failure.value?.requestId" :code="page.failure.value?.code" />
    <ErrorState v-else-if="page.failure.value" :failure="page.failure.value" @retry="page.reload" />
    <div v-else class="kp-stack">
      <EmptyState v-if="page.status.value === 'empty'" title="No accepted intents yet" description="Send a message through the API or SMTP and it will appear here with its identifier." />
      <DataTable v-else caption="Accepted message intents" :columns="columns" :rows="rows">
        <template #cell-id="{ value }"><code class="kp-code">{{ value }}</code></template>
        <template #cell-status="{ value }"><StatusBadge :value="String(value)" /></template>
        <template #cell-recipient="{ value }"><SafeText :value="value" /></template>
        <template #cell-created_at="{ value }"><time :datetime="String(value)">{{ new Date(String(value)).toLocaleString() }}</time></template>
      </DataTable>
      <PanelCard title="Example request" eyebrow="Copy-safe, credentials redacted">
        <pre class="kp-contract">{{ example }}</pre>
      </PanelCard>
      <PanelCard title="Operation log" eyebrow="Not available" source="unavailable">
        <UnavailableState title="Operation log" dependency="No browser API exists yet. Required contract: GET /app/api/developer/operations (request ID, operation, actor, result, timestamp) with cursor pagination." />
      </PanelCard>
    </div>
  </div>
</template>
