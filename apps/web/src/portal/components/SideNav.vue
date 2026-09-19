<script setup lang="ts">
import type { NavigationGroup } from '../access'

defineProps<{ groups: NavigationGroup[]; currentPath: string; collapsed: boolean; open: boolean; admin: boolean }>()
const emit = defineEmits<{ toggleCollapsed: []; close: [] }>()
function isCurrent(path: string, current: string) {
  return current === path || current.startsWith(path + '/')
}
</script>

<template>
  <div class="kp-drawer-backdrop" :data-open="open ? 'true' : 'false'" @click="emit('close')"></div>
  <aside id="kp-sidebar" class="kp-sidebar" :data-open="open ? 'true' : 'false'">
    <button type="button" class="kp-button kp-sidebar__toggle" :aria-expanded="collapsed ? 'false' : 'true'" @click="emit('toggleCollapsed')">
      <span aria-hidden="true">{{ collapsed ? '»' : '«' }}</span><span :class="{ 'kp-visually-hidden': collapsed }">{{ collapsed ? 'Expand navigation' : 'Collapse navigation' }}</span>
    </button>
    <nav :aria-label="admin ? 'Platform administration' : 'Product navigation'">
      <div v-for="group in groups" :key="group.group" class="kp-nav-group">
        <p class="kp-nav-group__label" :title="group.group">{{ group.group }}</p>
        <ul>
          <li v-for="item in group.items" :key="item.path">
            <a class="kp-nav-link" :href="item.path" :aria-current="isCurrent(item.path, currentPath) ? 'page' : undefined" :title="item.label" @click="emit('close')">
              <span class="kp-nav-link__initial" aria-hidden="true">{{ item.label.slice(0, 1) }}</span>
              <span class="kp-nav-link__label">{{ item.label }}</span>
              <span v-if="item.availability === 'unavailable'" class="kp-nav-link__hint" aria-label="not available yet">soon</span>
            </a>
          </li>
        </ul>
      </div>
      <div class="kp-nav-group">
        <p class="kp-nav-group__label">Shortcuts</p>
        <ul>
          <li><a class="kp-nav-link" href="/app/mail" data-external="true"><span class="kp-nav-link__initial" aria-hidden="true">W</span><span class="kp-nav-link__label">Webmail</span></a></li>
          <li v-if="!admin"><a class="kp-nav-link" href="/onboarding" data-external="true"><span class="kp-nav-link__initial" aria-hidden="true">S</span><span class="kp-nav-link__label">Setup guide</span></a></li>
          <li v-if="admin"><a class="kp-nav-link" href="/app/overview"><span class="kp-nav-link__initial" aria-hidden="true">C</span><span class="kp-nav-link__label">Customer workspace</span></a></li>
        </ul>
      </div>
    </nav>
  </aside>
</template>
