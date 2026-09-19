<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import { appApi, requireSession, type BrowserSession } from './api'
import { evaluateAccess, visibleNavigation, type AccessDecision, type AdminAuthority } from './portal/access'
import { probeAdminAuthority } from './portal/adminAuthority'
import { normalizeFailure, type PortalFailure } from './portal/errors'
import { currentLocation, installRouter, navigate } from './portal/router'
import { groupIndexPath, matchPortalRoute, type PortalMatch } from './portal/routes'
import { bindTenant, clearTenantState } from './portal/state'
import { notify } from './portal/toasts'
import { pageFor } from './portal/pages'
import './portal/portal.css'
import SkipLink from './portal/components/SkipLink.vue'
import TopBar from './portal/components/TopBar.vue'
import SideNav from './portal/components/SideNav.vue'
import Breadcrumbs from './portal/components/Breadcrumbs.vue'
import ToastRegion from './portal/components/ToastRegion.vue'
import LoadingState from './portal/components/LoadingState.vue'
import ErrorState from './portal/components/ErrorState.vue'
import ForbiddenState from './portal/components/ForbiddenState.vue'
import NotFoundPage from './portal/pages/NotFoundPage.vue'

interface Organization { tenant_id: string; name: string; role: string }

const session = ref<BrowserSession>({ authenticated: false })
const adminAuthority = ref<AdminAuthority>('unknown')
const organizations = ref<Organization[]>([])
const booting = ref(true)
const bootFailure = ref<PortalFailure | null>(null)
const match = shallowRef<PortalMatch | null>(null)
const decision = ref<AccessDecision>({ kind: 'pending' })
const collapsed = ref(false)
const drawerOpen = ref(false)
const switching = ref(false)
let disposeRouter: (() => void) | null = null

const isAdminArea = computed(() => currentLocation.path.startsWith('/admin/'))
// The admin shell only renders for server-proven administrators; everyone else keeps the tenant shell.
const adminShell = computed(() => isAdminArea.value && adminAuthority.value === 'proven')
const navigation = computed(() => visibleNavigation(session.value, adminAuthority.value).filter(group => adminShell.value ? group.group === 'Admin' : group.group !== 'Admin'))
const page = computed(() => (match.value ? pageFor(match.value.route) : null))
const breadcrumbs = computed(() => {
  if (!match.value) return [{ label: isAdminArea.value ? 'Administration' : 'Workspace', href: isAdminArea.value ? '/admin/system' : '/app/overview' }, { label: 'Not found' }]
  const route = match.value.route
  const index = groupIndexPath('/' + route.pattern.split('/').filter(Boolean).slice(0, 2).join('/'))
  const trail = [{ label: isAdminArea.value ? 'Administration' : 'Workspace', href: isAdminArea.value ? '/admin/system' : '/app/overview' }]
  if (route.group !== 'Overview' && route.group !== 'Admin') trail.push({ label: route.group, href: index || route.pattern })
  else if (route.group === 'Admin') trail.push({ label: 'Platform', href: '/admin/system' })
  trail.push({ label: route.breadcrumb, href: route.pattern })
  if (match.value.params.id) trail.push({ label: match.value.params.id, href: currentLocation.path })
  return trail
})

function resolveRoute() {
  const index = groupIndexPath(currentLocation.path)
  if (index) { navigate(index, true); return }
  match.value = matchPortalRoute(currentLocation.path)
  decision.value = match.value ? evaluateAccess(session.value, match.value.route, adminAuthority.value) : { kind: 'granted' }
  document.title = `${match.value ? match.value.route.title : 'Not found'} · Klyrow`
}

async function boot() {
  booting.value = true
  bootFailure.value = null
  try {
    session.value = await requireSession()
    bindTenant(session.value.tenant_id || '')
    organizations.value = (session.value.workspaces || []).map(item => ({ tenant_id: item.tenant_id, name: item.tenant_id === session.value.tenant_id ? 'Current organization' : item.tenant_id, role: item.role }))
    resolveRoute()
    void appApi<{ organizations?: Organization[] }>('/app/api/context').then(context => {
      if (context.organizations?.length) organizations.value = context.organizations
    }).catch(() => { /* the switcher falls back to session workspaces */ })
    adminAuthority.value = await probeAdminAuthority()
    resolveRoute()
  } catch (error) {
    bootFailure.value = normalizeFailure(error)
  } finally {
    booting.value = false
  }
}

async function switchOrganization(tenantId: string) {
  if (!tenantId || tenantId === session.value.tenant_id || switching.value) return
  switching.value = true
  try {
    await appApi(`/app/api/organizations/${encodeURIComponent(tenantId)}/switch`, { method: 'POST' })
    clearTenantState()
    // A full navigation guarantees no tenant-bound component state survives the switch.
    location.assign('/app/overview')
  } catch (error) {
    const failure = normalizeFailure(error)
    notify('error', 'The organization could not be switched.', failure.requestId)
    switching.value = false
  }
}

async function logout() {
  try {
    const result = await appApi<{ end_session_url?: string }>('/auth/logout', { method: 'POST' })
    location.assign(result?.end_session_url || '/logged-out')
  } catch {
    location.assign('/logged-out')
  }
}

function search(query: string) {
  const needle = query.trim().toLowerCase()
  if (!needle) return
  const hit = navigation.value.flatMap(group => group.items).find(item => item.label.toLowerCase().includes(needle))
  if (hit) navigate(hit.path)
  else notify('info', `No page matches “${query.slice(0, 60)}”.`)
}

watch(() => currentLocation.path, () => { drawerOpen.value = false; if (!booting.value) resolveRoute() })
onMounted(() => { disposeRouter = installRouter(); void boot() })
onBeforeUnmount(() => disposeRouter?.())
</script>

<template>
  <div class="kp-shell" :data-area="adminShell ? 'admin' : 'tenant'">
    <SkipLink />
    <TopBar
      :session="session" :admin-authority="adminAuthority" :organizations="organizations" :drawer-open="drawerOpen" :switching="switching" :admin="adminShell"
      @toggle-drawer="drawerOpen = !drawerOpen" @switch-organization="switchOrganization" @logout="logout" @search="search"
    />
    <div class="kp-layout" :data-collapsed="collapsed ? 'true' : 'false'">
      <SideNav :groups="navigation" :current-path="currentLocation.path" :collapsed="collapsed" :open="drawerOpen" :admin="adminShell" @toggle-collapsed="collapsed = !collapsed" @close="drawerOpen = false" />
      <main id="kp-main" class="kp-main" tabindex="-1">
        <Breadcrumbs :items="breadcrumbs" />
        <LoadingState v-if="booting" label="Loading your workspace…" />
        <ErrorState v-else-if="bootFailure" :failure="bootFailure" @retry="boot" />
        <NotFoundPage v-else-if="!match" :path="currentLocation.path" />
        <LoadingState v-else-if="decision.kind === 'pending'" label="Verifying platform authority…" />
        <ForbiddenState v-else-if="decision.kind === 'forbidden'" :reason="decision.reason" />
        <component :is="page" v-else-if="page" :key="currentLocation.path" :route="match.route" :params="match.params" :session="session" />
      </main>
    </div>
    <ToastRegion />
  </div>
</template>
