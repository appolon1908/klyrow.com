<script setup lang="ts">
import { computed } from 'vue'
import { appApi, type BrowserSession } from '../../api'
import type { PortalRoute } from '../routes'
import type { PlatformMetrics } from '../adminAuthority'
import { usePage } from '../composables/usePage'
import PageHeader from '../components/PageHeader.vue'
import MetricCard from '../components/MetricCard.vue'
import PanelCard from '../components/PanelCard.vue'
import LoadingState from '../components/LoadingState.vue'
import ErrorState from '../components/ErrorState.vue'
import ForbiddenState from '../components/ForbiddenState.vue'
import UnavailableState from '../components/UnavailableState.vue'

defineProps<{ route: PortalRoute; params: Record<string, string>; session: BrowserSession }>()
const page = usePage(() => appApi<PlatformMetrics>('/app/api/admin/dashboard'), { cacheKey: 'admin-dashboard' })
const source = computed(() => (page.stale.value ? 'stale' : 'live'))
</script>

<template>
  <div>
    <PageHeader title="System" eyebrow="Platform operations" description="Read-first platform counts from the durable stores that serve the product. No infrastructure topology or certification state is fabricated.">
      <button type="button" class="kp-button" :disabled="page.status.value === 'loading'" @click="page.reload">Refresh</button>
    </PageHeader>
    <LoadingState v-if="page.status.value === 'loading'" label="Loading platform counts…" />
    <ForbiddenState v-else-if="page.status.value === 'forbidden'" reason="platform-admin" :request-id="page.failure.value?.requestId" :code="page.failure.value?.code" />
    <ErrorState v-else-if="page.failure.value" :failure="page.failure.value" @retry="page.reload" />
    <div v-else-if="page.data.value" class="kp-stack">
      <section class="kp-grid" aria-label="Platform counts">
        <MetricCard label="Tenants" :value="page.data.value.tenants" detail="Provisioned organizations" :source="source" />
        <MetricCard label="Users" :value="page.data.value.users" detail="Application identities" :source="source" />
        <MetricCard label="Messages" :value="page.data.value.messages" detail="Recorded email intents" :source="source" />
        <MetricCard label="Verified domains" :value="page.data.value.verified_domains" detail="Approved sending domains" :source="source" />
        <MetricCard label="Outbox active" :value="page.data.value.outbox_active" detail="Pending, sending or retrying" :source="source" />
        <MetricCard label="Outbox failed" :value="page.data.value.outbox_failed" detail="Exhausted deliveries" :source="source" />
        <MetricCard label="Webhook endpoints" :value="page.data.value.webhooks" detail="Customer subscriptions" :source="source" />
        <MetricCard label="Usage ledger rows" :value="page.data.value.usage_events" detail="Billing usage events" :source="source" />
      </section>
      <PanelCard title="Certification and topology" eyebrow="Not available" source="unavailable">
        <UnavailableState title="Release certification evidence" dependency="No browser API exists yet. Required contract: GET /app/api/admin/system/certification (release identity, image digests, migration ledger, evidence checksums)." :alternatives="[{ label: 'Provisioning operations', href: '/admin/provisioning' }]" />
      </PanelCard>
    </div>
  </div>
</template>
