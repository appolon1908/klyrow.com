<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { appApi, type BrowserSession } from '../../api'
import type { PortalRoute } from '../routes'
import type { MessageRow } from '../types'
import { usePage } from '../composables/usePage'
import PageHeader from '../components/PageHeader.vue'
import FilterBar from '../components/FilterBar.vue'
import SearchInput from '../components/SearchInput.vue'
import DataTable from '../components/DataTable.vue'
import PaginationControls from '../components/PaginationControls.vue'
import StatusBadge from '../components/StatusBadge.vue'
import LoadingState from '../components/LoadingState.vue'
import EmptyState from '../components/EmptyState.vue'
import ErrorState from '../components/ErrorState.vue'
import ForbiddenState from '../components/ForbiddenState.vue'
import SafeText from '../components/SafeText.vue'
import SourceBadge from '../components/SourceBadge.vue'

defineProps<{ route: PortalRoute; params: Record<string, string>; session: BrowserSession }>()
const LIMIT = 50
const offset = ref(0)
const status = ref('')
const query = ref('')
const page = usePage(() => appApi<MessageRow[]>(`/app/api/messages?limit=${LIMIT}&offset=${offset.value}`), { isEmpty: rows => rows.length === 0 && offset.value === 0 })
watch(offset, () => { void page.reload() })
const statuses = computed(() => Array.from(new Set((page.data.value || []).map(row => row.status))).sort())
const rows = computed(() => (page.data.value || []).filter(row => (!status.value || row.status === status.value) &&
  (!query.value || [row.recipient, row.sender, row.subject, row.id].some(value => String(value || '').toLowerCase().includes(query.value.toLowerCase())))))
const columns = [{ key: 'subject', label: 'Subject' }, { key: 'recipient', label: 'Recipient' }, { key: 'sender', label: 'Sender' }, { key: 'status', label: 'Status' }, { key: 'created_at', label: 'Created' }]
</script>

<template>
  <div>
    <PageHeader title="Messages" eyebrow="Email operations" description="Accepted message intents for this organization, newest first. Filters apply to the loaded page; server-side search is not available yet.">
      <button type="button" class="kp-button" :disabled="page.status.value === 'loading'" @click="page.reload">Refresh</button>
      <a class="kp-button" href="/app/mail" data-external="true">Open webmail</a>
    </PageHeader>
    <LoadingState v-if="page.status.value === 'loading'" label="Loading messages…" :lines="5" />
    <ForbiddenState v-else-if="page.status.value === 'forbidden'" reason="server" :request-id="page.failure.value?.requestId" :code="page.failure.value?.code" />
    <ErrorState v-else-if="page.failure.value" :failure="page.failure.value" @retry="page.reload" />
    <EmptyState v-else-if="page.status.value === 'empty'" title="No messages recorded yet" description="Messages accepted through the API, SMTP or webmail appear here with their delivery status.">
      <a class="kp-button--primary" href="/app">Send a transactional email</a>
    </EmptyState>
    <div v-else class="kp-stack">
      <FilterBar>
        <SearchInput v-model="query" id="message-search" label="Search" placeholder="Recipient, sender, subject or ID" />
        <div class="kp-field">
          <label for="message-status">Status</label>
          <select id="message-status" v-model="status">
            <option value="">All statuses</option>
            <option v-for="item in statuses" :key="item" :value="item">{{ item }}</option>
          </select>
        </div>
        <SourceBadge :source="page.stale.value ? 'stale' : 'live'" />
      </FilterBar>
      <DataTable caption="Messages" :columns="columns" :rows="rows" empty-message="No messages match the current filters on this page.">
        <template #cell-subject="{ row }"><a :href="`/app/email/messages/${encodeURIComponent(String(row.id))}`"><SafeText :value="row.subject" fallback="(no subject)" /></a></template>
        <template #cell-status="{ value }"><StatusBadge :value="String(value)" /></template>
        <template #cell-created_at="{ value }"><time :datetime="String(value)">{{ new Date(String(value)).toLocaleString() }}</time></template>
      </DataTable>
      <PaginationControls :offset="offset" :limit="LIMIT" :count="(page.data.value || []).length" :has-more="(page.data.value || []).length === LIMIT" @previous="offset = Math.max(0, offset - LIMIT)" @next="offset += LIMIT" />
    </div>
  </div>
</template>
