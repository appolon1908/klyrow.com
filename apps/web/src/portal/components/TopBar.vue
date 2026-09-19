<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import type { BrowserSession } from '../../session'
import type { AdminAuthority } from '../access'

const props = defineProps<{ session: BrowserSession; adminAuthority: AdminAuthority; organizations: Array<{ tenant_id: string; name: string; role: string }>; drawerOpen: boolean; switching: boolean; admin: boolean }>()
const emit = defineEmits<{ toggleDrawer: []; switchOrganization: [tenantId: string]; logout: []; search: [query: string] }>()
const menuOpen = ref(false)
const query = ref('')
const menu = ref<HTMLElement | null>(null)
const environment = computed(() => {
  const host = location.hostname
  if (host === 'app.klyrow.com') return 'production'
  if (host.includes('staging')) return 'staging'
  return 'development'
})
const displayName = computed(() => props.session.email || 'Klyrow user')
function onDocumentClick(event: Event) {
  if (menu.value && !menu.value.contains(event.target as Node)) menuOpen.value = false
}
function onKey(event: KeyboardEvent) {
  if (event.key === 'Escape') menuOpen.value = false
}
onMounted(() => { document.addEventListener('click', onDocumentClick); document.addEventListener('keydown', onKey) })
onBeforeUnmount(() => { document.removeEventListener('click', onDocumentClick); document.removeEventListener('keydown', onKey) })
</script>

<template>
  <header class="kp-topbar">
    <button type="button" class="kp-icon-button kp-menu-toggle" :aria-expanded="drawerOpen ? 'true' : 'false'" aria-controls="kp-sidebar" :aria-label="drawerOpen ? 'Close navigation' : 'Open navigation'" @click="emit('toggleDrawer')">
      <span aria-hidden="true">☰</span>
    </button>
    <a class="kp-brand" :href="admin ? '/admin/system' : '/app/overview'" :aria-label="admin ? 'Klyrow administration home' : 'Klyrow home'">
      <span class="kp-brand__mark" aria-hidden="true">K</span>
      <span class="kp-brand__text">Klyrow</span>
      <span v-if="admin" class="kp-brand__admin">ADMIN</span>
    </a>
    <div class="kp-org-switcher">
      <label class="kp-visually-hidden" for="kp-organization">Organization</label>
      <select id="kp-organization" :value="session.tenant_id" :disabled="switching || organizations.length < 2" @change="emit('switchOrganization', ($event.target as HTMLSelectElement).value)">
        <option v-for="item in organizations" :key="item.tenant_id" :value="item.tenant_id">{{ item.name }} · {{ item.role }}</option>
      </select>
    </div>
    <span class="kp-env" :data-env="environment">{{ environment }}</span>
    <div class="kp-topbar__spacer"></div>
    <form class="kp-search" role="search" @submit.prevent="emit('search', query)">
      <span aria-hidden="true">⌕</span>
      <label class="kp-visually-hidden" for="kp-global-search">Search</label>
      <input id="kp-global-search" v-model="query" type="search" placeholder="Search pages" autocomplete="off">
    </form>
    <a class="kp-icon-button" href="/app/support" aria-label="Help and support">?</a>
    <div ref="menu" class="kp-menu">
      <button type="button" class="kp-button" aria-haspopup="menu" :aria-expanded="menuOpen ? 'true' : 'false'" @click="menuOpen = !menuOpen">
        <span class="kp-visually-hidden">Account menu for </span>
        <span class="kp-account__initial" aria-hidden="true">{{ displayName.slice(0, 1).toUpperCase() }}</span>
        <span class="kp-account__name">{{ displayName }}</span>
      </button>
      <div v-if="menuOpen" class="kp-menu__list" role="menu" aria-label="Account">
        <div class="kp-menu__meta"><strong>{{ displayName }}</strong>{{ session.role }} · {{ session.tenant_id }}</div>
        <a role="menuitem" href="/app/settings/security">Security &amp; sessions</a>
        <a role="menuitem" href="/app/mail">Webmail</a>
        <a role="menuitem" href="/app">Legacy command center</a>
        <a v-if="adminAuthority === 'proven'" role="menuitem" href="/admin/system">Platform administration</a>
        <button type="button" role="menuitem" @click="emit('logout')">Sign out</button>
      </div>
    </div>
  </header>
</template>
