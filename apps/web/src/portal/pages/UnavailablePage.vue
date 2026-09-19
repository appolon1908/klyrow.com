<script setup lang="ts">
import { computed } from 'vue'
import type { PortalRoute } from '../routes'
import PageHeader from '../components/PageHeader.vue'
import UnavailableState from '../components/UnavailableState.vue'

const props = defineProps<{ route: PortalRoute; params: Record<string, string> }>()
const ALTERNATIVES: Record<string, Array<{ label: string; href: string }>> = {
  Email: [{ label: 'Messages', href: '/app/email/messages' }, { label: 'Domains', href: '/app/email/domains' }],
  Content: [{ label: 'Send a transactional email', href: '/app' }],
  Audience: [{ label: 'Suppression counts on overview', href: '/app/overview' }],
  Campaigns: [{ label: 'Overview', href: '/app/overview' }],
  Journeys: [{ label: 'Overview', href: '/app/overview' }],
  Analytics: [{ label: 'Analytics overview', href: '/app/analytics/overview' }],
  Deliverability: [{ label: 'Domain deliverability', href: '/app/deliverability' }],
  Developer: [{ label: 'Message logs', href: '/app/developer/logs' }],
  Billing: [{ label: 'Plan', href: '/app/billing/plan' }, { label: 'Usage', href: '/app/billing/usage' }],
  Settings: [{ label: 'Organization', href: '/app/settings/organization' }, { label: 'Security', href: '/app/settings/security' }],
  Support: [{ label: 'Overview', href: '/app/overview' }],
  Admin: [{ label: 'System', href: '/admin/system' }, { label: 'Provisioning operations', href: '/admin/provisioning' }],
}
const alternatives = computed(() => (ALTERNATIVES[props.route.group] || []).filter(item => item.href !== props.route.pattern))
</script>

<template>
  <div>
    <PageHeader :title="route.title" :eyebrow="route.group" />
    <UnavailableState :title="route.title" :dependency="route.dependency" :alternatives="alternatives">
      <p v-if="params.id" class="kp-muted">Requested record: <code class="kp-code">{{ params.id }}</code></p>
    </UnavailableState>
  </div>
</template>
