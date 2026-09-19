<script setup lang="ts">
import { computed } from 'vue'
import { appApi, type BrowserSession } from '../../api'
import type { PortalRoute } from '../routes'
import type { PlatformMetrics } from '../adminAuthority'
import type { ProvisioningStatus } from '../types'
import { settle } from '../composables/usePage'
import { onMounted, ref } from 'vue'
import type { PortalFailure } from '../errors'
import PageHeader from '../components/PageHeader.vue'
import MetricCard from '../components/MetricCard.vue'
import PanelCard from '../components/PanelCard.vue'
import DataTable from '../components/DataTable.vue'
import StatusBadge from '../components/StatusBadge.vue'
import LoadingState from '../components/LoadingState.vue'
import ErrorState from '../components/ErrorState.vue'
import ForbiddenState from '../components/ForbiddenState.vue'
import UnavailableState from '../components/UnavailableState.vue'
import DegradedState from '../components/DegradedState.vue'
import SafeText from '../components/SafeText.vue'

// Shared read-first page for admin tenants, queues and deliverability: counts from
// the admin dashboard API plus the honest missing contract for everything else.
const props = defineProps<{ route: PortalRoute; params: Record<string, string>; session: BrowserSession }>()
const loading = ref(true)
const metrics = ref<PlatformMetrics | null>(null)
const failure = ref<PortalFailure | null>(null)
const failures = ref<ProvisioningStatus[]>([])
const provisioningFailure = ref<PortalFailure | null>(null)
const kind = computed(() => props.route.name)
const failureColumns = [{ key: 'tenant_id', label: 'Tenant' }, { key: 'state', label: 'State' }, { key: 'attempts', label: 'Attempts' }, { key: 'last_error', label: 'Last error' }]

async function load() {
  loading.value = true
  const result = await settle({
    metrics: () => appApi<PlatformMetrics>('/app/api/admin/dashboard'),
    provisioning: () => (kind.value === 'admin-deliverability' ? appApi<ProvisioningStatus[]>('/app/api/admin/provisioning/postal') : Promise.resolve([] as ProvisioningStatus[])),
  })
  metrics.value = result.metrics.value
  failure.value = result.metrics.failure
  failures.value = result.provisioning.value || []
  provisioningFailure.value = result.provisioning.failure
  loading.value = false
}
onMounted(load)
</script>

<template>
  <div>
    <PageHeader :title="route.title" eyebrow="Platform operations" :description="route.dependency">
      <button type="button" class="kp-button" :disabled="loading" @click="load">Refresh</button>
    </PageHeader>
    <LoadingState v-if="loading" label="Loading platform counts…" />
    <ForbiddenState v-else-if="failure?.kind === 'forbidden'" reason="platform-admin" :request-id="failure.requestId" :code="failure.code" />
    <ErrorState v-else-if="failure" :failure="failure" @retry="load" />
    <div v-else-if="metrics" class="kp-stack">
      <section v-if="kind === 'admin-tenants'" class="kp-grid" aria-label="Tenant counts">
        <MetricCard label="Tenants" :value="metrics.tenants" detail="Provisioned organizations" source="live" />
        <MetricCard label="Users" :value="metrics.users" detail="Application identities" source="live" />
        <MetricCard label="Verified domains" :value="metrics.verified_domains" source="live" />
      </section>
      <section v-else-if="kind === 'admin-queues'" class="kp-grid" aria-label="Queue counts">
        <MetricCard label="Outbox active" :value="metrics.outbox_active" detail="Pending, sending or retrying" source="live" />
        <MetricCard label="Outbox failed" :value="metrics.outbox_failed" detail="Exhausted deliveries" source="live" />
        <MetricCard label="Messages" :value="metrics.messages" detail="Recorded intents" source="live" />
      </section>
      <section v-else class="kp-grid" aria-label="Deliverability counts">
        <MetricCard label="Verified domains" :value="metrics.verified_domains" source="live" />
        <MetricCard label="Outbox failed" :value="metrics.outbox_failed" source="live" />
        <MetricCard label="Blocked provisioning" :value="provisioningFailure ? null : failures.length" :source="provisioningFailure ? 'degraded' : 'live'" />
      </section>
      <PanelCard v-if="kind === 'admin-deliverability'" title="Provisioning failures" eyebrow="Postal" :source="provisioningFailure ? 'degraded' : 'live'">
        <template #actions><a class="kp-button" href="/admin/provisioning" data-external="true">Provisioning operations</a></template>
        <DegradedState v-if="provisioningFailure" message="Provisioning failures could not be read." :request-id="provisioningFailure.requestId" :code="provisioningFailure.code" />
        <DataTable v-else caption="Blocked or incomplete provisioning" :columns="failureColumns" :rows="failures" row-key="tenant_id" empty-message="No blocked or incomplete Postal mappings.">
          <template #cell-tenant_id="{ value }"><code class="kp-code">{{ value }}</code></template>
          <template #cell-state="{ value }"><StatusBadge :value="String(value)" /></template>
          <template #cell-last_error="{ value }"><SafeText :value="value" fallback="none recorded" /></template>
        </DataTable>
      </PanelCard>
      <PanelCard :title="kind === 'admin-tenants' ? 'Tenant listing' : kind === 'admin-queues' ? 'Queue topology' : 'Platform DNS and reputation'" eyebrow="Not available" source="unavailable">
        <UnavailableState :title="kind === 'admin-tenants' ? 'Tenant listing' : kind === 'admin-queues' ? 'Queue topology' : 'Platform DNS and reputation'" :dependency="kind === 'admin-tenants' ? 'No browser API exists yet. Required contract: GET /app/api/admin/tenants (id, name, status, plan, verified domains) with explicit tenant context per row.' : kind === 'admin-queues' ? 'No browser API exists yet. Required contract: GET /app/api/admin/queues (per-worker leases, retry backlog, DLQ depth, oldest item age). No infrastructure status is fabricated.' : 'No browser API exists yet. Required contract: GET /app/api/admin/deliverability (platform DNS checks, IP reputation, warmup state).'" />
      </PanelCard>
    </div>
  </div>
</template>
