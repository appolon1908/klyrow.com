<script setup lang="ts">
import { ref } from 'vue'
import { appApi, type BrowserSession } from '../../api'
import type { PortalRoute } from '../routes'
import type { AdminAuditEntry, AdminAuditPage } from '../types'
import { normalizeFailure, type PortalFailure } from '../errors'
import PageHeader from '../components/PageHeader.vue'
import PanelCard from '../components/PanelCard.vue'
import DataTable from '../components/DataTable.vue'
import LoadingState from '../components/LoadingState.vue'
import ErrorState from '../components/ErrorState.vue'
import ForbiddenState from '../components/ForbiddenState.vue'

defineProps<{ route: PortalRoute; params: Record<string, string>; session: BrowserSession }>()

const loading = ref(true)
const loadingMore = ref(false)
const failure = ref<PortalFailure | null>(null)
const items = ref<AdminAuditEntry[]>([])
const nextCursor = ref<string | null>(null)
const tenantId = ref('')
const actor = ref('')
const actionPrefix = ref('')

const columns = [
  { key: 'created_at', label: 'Time' },
  { key: 'tenant_name', label: 'Tenant' },
  { key: 'actor', label: 'Actor' },
  { key: 'action', label: 'Action' },
]

function query(cursor?: string | null) {
  const params = new URLSearchParams()
  if (tenantId.value.trim()) params.set('tenant_id', tenantId.value.trim())
  if (actor.value.trim()) params.set('actor', actor.value.trim())
  if (actionPrefix.value.trim()) params.set('action_prefix', actionPrefix.value.trim())
  if (cursor) params.set('cursor', cursor)
  params.set('limit', '100')
  return '/app/api/admin/audit?' + params.toString()
}

function date(value: unknown) {
  if (!value) return '—'
  const parsed = new Date(String(value))
  return Number.isNaN(parsed.getTime()) ? '—' : parsed.toLocaleString()
}

async function load(reset = true) {
  if (reset) loading.value = true
  else loadingMore.value = true
  failure.value = null
  try {
    const page = await appApi<AdminAuditPage>(query(reset ? null : nextCursor.value))
    items.value = reset ? page.items : [...items.value, ...page.items]
    nextCursor.value = page.next_cursor || null
  } catch (error) {
    failure.value = normalizeFailure(error)
  } finally {
    loading.value = false
    loadingMore.value = false
  }
}

function clearFilters() {
  tenantId.value = ''
  actor.value = ''
  actionPrefix.value = ''
  void load(true)
}

load(true)
</script>

<template>
  <div>
    <PageHeader title="Platform audit" eyebrow="Platform operations" description="Read the append-only application audit trail across tenants. The portal exposes identifiers and action codes only; secrets and payload bodies are not part of this contract.">
      <button type="button" class="kp-button" :disabled="loading" @click="load(true)">Refresh</button>
    </PageHeader>

    <PanelCard title="Filters" eyebrow="Read-only">
      <form class="kp-form" aria-label="Filter platform audit" @submit.prevent="load(true)">
        <div class="kp-grid">
          <div class="kp-field"><label for="audit-tenant">Tenant ID</label><input id="audit-tenant" v-model="tenantId" /></div>
          <div class="kp-field"><label for="audit-actor">Actor ID</label><input id="audit-actor" v-model="actor" /></div>
          <div class="kp-field"><label for="audit-action">Action prefix</label><input id="audit-action" v-model="actionPrefix" placeholder="billing." /></div>
        </div>
        <div class="kp-form__actions">
          <button type="submit" class="kp-button--primary" :disabled="loading">Apply filters</button>
          <button type="button" class="kp-button" @click="clearFilters">Clear</button>
        </div>
      </form>
    </PanelCard>

    <LoadingState v-if="loading" label="Loading audit records…" />
    <ForbiddenState v-else-if="failure?.kind === 'forbidden'" reason="platform-admin" :request-id="failure.requestId" :code="failure.code" />
    <ErrorState v-else-if="failure" :failure="failure" @retry="load(true)" />
    <PanelCard v-else title="Audit records" eyebrow="Newest first">
      <DataTable caption="Platform audit records" :columns="columns" :rows="items" empty-message="No audit records match these filters.">
        <template #cell-created_at="{ value }">{{ date(value) }}</template>
        <template #cell-tenant_name="{ row }"><span>{{ row.tenant_name || row.tenant_id }}</span></template>
        <template #cell-actor="{ value }"><code class="kp-code">{{ value }}</code></template>
        <template #cell-action="{ value }"><code class="kp-code">{{ value }}</code></template>
      </DataTable>
      <div v-if="nextCursor" class="kp-inline-actions">
        <button type="button" class="kp-button" :disabled="loadingMore" @click="load(false)">{{ loadingMore ? 'Loading…' : 'Load more' }}</button>
      </div>
    </PanelCard>
  </div>
</template>
