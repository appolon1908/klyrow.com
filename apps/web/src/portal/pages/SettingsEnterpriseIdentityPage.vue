<script setup lang="ts">
import { computed } from 'vue'
import { appApi, type BrowserSession } from '../../api'
import type { PortalRoute } from '../routes'
import { usePage } from '../composables/usePage'
import PageHeader from '../components/PageHeader.vue'
import PanelCard from '../components/PanelCard.vue'
import LoadingState from '../components/LoadingState.vue'
import ErrorState from '../components/ErrorState.vue'
import ForbiddenState from '../components/ForbiddenState.vue'
import StatusBadge from '../components/StatusBadge.vue'

interface IdentityCapabilities {
  identity_authority:string
  browser_session_authority:string
  sso:{configured:boolean;mutation_available:boolean;dependency:string}
  scim:{configured:boolean;mutation_available:boolean;dependency:string}
  runtime_certification:string
  direct_keycloak_writes:boolean
}
defineProps<{route:PortalRoute;params:Record<string,string>;session:BrowserSession}>()
const page=usePage(()=>appApi<IdentityCapabilities>('/app/api/identity/capabilities'),{cacheKey:'identity-capabilities'})
const source=computed(()=>page.stale.value?'stale':'live')
</script>
<template><div>
<PageHeader title="Enterprise identity" eyebrow="Settings" description="Readiness and authority for SSO and SCIM. Identity changes are never written directly to Keycloak from this browser." />
<LoadingState v-if="page.status.value==='loading'" label="Loading identity capabilities…" />
<ForbiddenState v-else-if="page.status.value==='forbidden'" reason="management-role" :request-id="page.failure.value?.requestId" :code="page.failure.value?.code" />
<ErrorState v-else-if="page.failure.value" :failure="page.failure.value" @retry="page.reload" />
<div v-else-if="page.data.value" class="kp-stack">
<PanelCard title="Authority" eyebrow="Identity boundary" :source="source"><p><strong>{{page.data.value.identity_authority}}</strong> owns human identity. {{page.data.value.browser_session_authority}} owns the browser-session projection.</p><p>Direct Keycloak writes: <StatusBadge :value="page.data.value.direct_keycloak_writes?'enabled':'disabled'" /></p></PanelCard>
<PanelCard title="Single sign-on" eyebrow="Enterprise" :source="source"><p><StatusBadge :value="page.data.value.sso.configured?'configured':'not configured'" /> Configuration mutation: {{page.data.value.sso.mutation_available?'available':'not available'}}.</p><p>{{page.data.value.sso.dependency}}</p></PanelCard>
<PanelCard title="SCIM provisioning" eyebrow="Enterprise" :source="source"><p><StatusBadge :value="page.data.value.scim.configured?'configured':'not configured'" /> Configuration mutation: {{page.data.value.scim.mutation_available?'available':'not available'}}.</p><p>{{page.data.value.scim.dependency}}</p></PanelCard>
<PanelCard title="Runtime certification" eyebrow="Security gate" :source="source"><StatusBadge :value="page.data.value.runtime_certification" /><p>Runtime certification remains a separate PAS-192/PAS-194 gate and is not self-certified by this product surface.</p></PanelCard>
</div></div></template>