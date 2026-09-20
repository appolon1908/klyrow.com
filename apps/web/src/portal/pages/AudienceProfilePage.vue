<script setup lang="ts">
import { computed } from 'vue'
import { appApi, type BrowserSession } from '../../api'
import type { PortalRoute } from '../routes'
import { usePage } from '../composables/usePage'
import PageHeader from '../components/PageHeader.vue'
import DataTable from '../components/DataTable.vue'
import LoadingState from '../components/LoadingState.vue'
import ErrorState from '../components/ErrorState.vue'
import ForbiddenState from '../components/ForbiddenState.vue'
import SafeText from '../components/SafeText.vue'

type ProfileDetail = {
  id: string
  email?: string | null
  phone?: string | null
  external_id?: string | null
  customer_id?: string | null
  attributes: Record<string, unknown>
  timeline: Array<{ id: string; name: string; source: string; occurred_at: string; properties: Record<string, unknown> }>
  consent: Array<{ topic: string; status: string; source: string; version: string; occurred_at: string }>
  preferences: Array<{ topic: string; subscribed: boolean; updated_at: string }>
}

const props = defineProps<{ route: PortalRoute; params: Record<string, string>; session: BrowserSession }>()
const page = usePage(() => appApi<ProfileDetail>(`/app/api/profiles/${encodeURIComponent(props.params.id || '')}`))
const attributes = computed(() => Object.entries(page.data.value?.attributes || {}).map(([key, value]) => ({ key, value: typeof value === 'string' ? value : JSON.stringify(value) })))
</script>

<template>
  <div>
    <PageHeader title="Profile" eyebrow="Audience" description="Tenant-scoped identity, consent, preferences, and event history.">
      <a class="kp-button" href="/app/audience/profiles">Back to profiles</a>
      <button type="button" class="kp-button" :disabled="page.status.value === 'loading'" @click="page.reload">Refresh</button>
    </PageHeader>
    <LoadingState v-if="page.status.value === 'loading'" label="Loading profile…" />
    <ForbiddenState v-else-if="page.status.value === 'forbidden'" reason="server" :request-id="page.failure.value?.requestId" :code="page.failure.value?.code" />
    <ErrorState v-else-if="page.failure.value" :failure="page.failure.value" @retry="page.reload" />
    <div v-else-if="page.data.value" class="kp-stack">
      <section class="kp-panel">
        <h2>Identity</h2>
        <dl class="kp-definition-list">
          <dt>Email</dt><dd><SafeText :value="page.data.value.email" /></dd>
          <dt>Phone</dt><dd><SafeText :value="page.data.value.phone" /></dd>
          <dt>External ID</dt><dd><SafeText :value="page.data.value.external_id" /></dd>
          <dt>Customer ID</dt><dd><SafeText :value="page.data.value.customer_id" /></dd>
        </dl>
      </section>
      <section class="kp-panel">
        <h2>Attributes</h2>
        <DataTable caption="Profile attributes" :columns="[{ key: 'key', label: 'Key' }, { key: 'value', label: 'Value' }]" :rows="attributes" row-key="key">
          <template #cell-key="{ value }"><SafeText :value="value" /></template>
          <template #cell-value="{ value }"><SafeText :value="value" /></template>
        </DataTable>
      </section>
      <section class="kp-panel">
        <h2>Timeline</h2>
        <DataTable caption="Profile timeline" :columns="[{ key: 'name', label: 'Event' }, { key: 'source', label: 'Source' }, { key: 'occurred_at', label: 'Occurred at' }]" :rows="page.data.value.timeline" row-key="id">
          <template #cell-name="{ value }"><SafeText :value="value" /></template>
          <template #cell-source="{ value }"><SafeText :value="value" /></template>
        </DataTable>
      </section>
    </div>
  </div>
</template>
