<script setup lang="ts">
import { computed } from 'vue'

export type DataSource = 'live' | 'stale' | 'unavailable' | 'not-configured' | 'forbidden' | 'degraded'
const props = defineProps<{ source: DataSource }>()
const tone = computed(() => ({ live: 'success', stale: 'warning', unavailable: 'info', 'not-configured': '', forbidden: 'danger', degraded: 'warning' })[props.source])
</script>

<template>
  <span class="kp-badge" :data-tone="tone" :title="`Data source: ${source.replace('-', ' ')}`">{{ source.replace('-', ' ') }}</span>
</template>
