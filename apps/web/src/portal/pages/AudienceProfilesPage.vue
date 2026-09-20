<script setup lang="ts">
import { computed } from 'vue'
import { appApi, type BrowserSession } from '../../api'
import type { PortalRoute } from '../routes'
import { usePage } from '../composables/usePage'
import PageHeader from '../components/PageHeader.vue'
import DataTable from '../components/DataTable.vue'
import LoadingState from '../components/LoadingState.vue'
import EmptyState from '../components/EmptyState.vue'
import ErrorState from '../components/ErrorState.vue'
import ForbiddenState from '../components/ForbiddenState.vue'
import SafeText from '../components/SafeText.vue'

type Profile = {
  id: string
  email?: string | null
  phone?: string | null
  external_id?: string | null
  customer_id?: string | null
  attributes: Record<string, unknown>
}

type ProfileResponse = { items: Profile[]; next_cursor: string | null }

const props = defineProps<{ route: PortalRoute; params: Record<string, string>; session: BrowserSession }>()
const page = usePage(() => appApi<ProfileResponse>('/app/api/profiles?limit=50'), { isEmpty: result => result.items.length === 0 })
const rows = computed(() => page.data.value?.items || [])
const columns = [
  { key: 'email', label: 'Email' },
  { key: 'phone', label: 'Phone' },
  { key: 'external_id', label: 'External ID' },
  { key: 'customer_id', label: 'Customer ID' },
]
</script>

<template>
  <div>
    <PageHeader title="Profiles" eyebrow="Audience" description="Tenant-scoped contact profiles and their durable consent, preference, and event history.">
      <button type="button" class="kp-button" :disabled="page.status.value === 'loading'" @click="page.reload">Refresh</button>
    </PageHeader>
    <LoadingState v-if="page.status.value === 'loading'" label="Loading profiles…" />
    <ForbiddenState v-else-if="page.status.value === 'forbidden'" reason="server" :request-id="page.failure.value?.requestId" :code="page.failure.value?.code" />
    <ErrorState v-else-if="page.failure.value" :failure="page.failure.value" @retry="page.reload" />
    <EmptyState v-else-if="page.status.value === 'empty'" title="No profiles yet" description="Profiles will appear here when tenant-scoped contacts are created." />
    <DataTable v-else caption="Profiles" :columns="columns" :rows="rows" row-key="id">
      <template #cell-email="{ value }"><SafeText :value="value" /></template>
      <template #cell-phone="{ value }"><SafeText :value="value" /></template>
      <template #cell-external_id="{ value }"><SafeText :value="value" /></template>
      <template #cell-customer_id="{ value }"><SafeText :value="value" /></template>
    </DataTable>
  </div>
</template>
