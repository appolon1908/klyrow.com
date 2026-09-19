<script setup lang="ts">
import { computed } from 'vue'
import { appApi, type BrowserSession } from '../../api'
import type { PortalRoute } from '../routes'
import type { BrowserContext } from '../types'
import { usePage } from '../composables/usePage'
import PageHeader from '../components/PageHeader.vue'
import PanelCard from '../components/PanelCard.vue'
import DataTable from '../components/DataTable.vue'
import StatusBadge from '../components/StatusBadge.vue'
import LoadingState from '../components/LoadingState.vue'
import ErrorState from '../components/ErrorState.vue'
import ForbiddenState from '../components/ForbiddenState.vue'
import UnavailableState from '../components/UnavailableState.vue'
import SafeText from '../components/SafeText.vue'

defineProps<{ route: PortalRoute; params: Record<string, string>; session: BrowserSession }>()
const page = usePage(() => appApi<BrowserContext>('/app/api/context'), { cacheKey: 'context' })
const current = computed(() => page.data.value?.organizations.find(item => item.tenant_id === page.data.value?.tenant) || null)
const columns = [{ key: 'name', label: 'Organization' }, { key: 'slug', label: 'Slug' }, { key: 'role', label: 'Your role' }, { key: 'enabled', label: 'Enabled' }]
</script>

<template>
  <div>
    <PageHeader title="Organization" eyebrow="Settings" description="Identity of the current organization and every membership on your account, as resolved by the server session.">
      <button type="button" class="kp-button" :disabled="page.status.value === 'loading'" @click="page.reload">Refresh</button>
    </PageHeader>
    <LoadingState v-if="page.status.value === 'loading'" label="Loading organization…" />
    <ForbiddenState v-else-if="page.status.value === 'forbidden'" reason="server" :request-id="page.failure.value?.requestId" :code="page.failure.value?.code" />
    <ErrorState v-else-if="page.failure.value" :failure="page.failure.value" @retry="page.reload" />
    <div v-else-if="page.data.value" class="kp-stack">
      <PanelCard title="Current organization" eyebrow="Identity" :source="page.stale.value ? 'stale' : 'live'">
        <dl class="kp-dl">
          <div><dt>Name</dt><dd><SafeText :value="current?.name" fallback="Not available" /></dd></div>
          <div><dt>Slug</dt><dd><SafeText :value="current?.slug" fallback="Not available" /></dd></div>
          <div><dt>Organization ID</dt><dd><code class="kp-code">{{ page.data.value.tenant }}</code></dd></div>
          <div><dt>Your role</dt><dd><StatusBadge :value="page.data.value.role" /></dd></div>
          <div><dt>Profile email</dt><dd><SafeText :value="page.data.value.profile?.email" /> <StatusBadge v-if="page.data.value.profile" :value="page.data.value.profile.email_verified ? 'verified' : 'pending'" /></dd></div>
          <div><dt>Display name</dt><dd><SafeText :value="page.data.value.profile?.display_name" /></dd></div>
        </dl>
      </PanelCard>
      <PanelCard title="Memberships" eyebrow="Organizations">
        <DataTable caption="Organization memberships" :columns="columns" :rows="page.data.value.organizations" row-key="tenant_id">
          <template #cell-role="{ value }"><StatusBadge :value="String(value)" /></template>
          <template #cell-enabled="{ value }"><StatusBadge :value="Boolean(value)" /></template>
        </DataTable>
      </PanelCard>
      <PanelCard title="Organization edit" eyebrow="Not available" source="unavailable">
        <UnavailableState title="Organization edit" dependency="No browser API exists yet. Required contract: PATCH /app/api/organization (name, slug, locale) restricted to owners." />
      </PanelCard>
    </div>
  </div>
</template>
