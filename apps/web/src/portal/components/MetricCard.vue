<script setup lang="ts">
import SourceBadge, { type DataSource } from './SourceBadge.vue'

defineProps<{ label: string; value: string | number | null | undefined; detail?: string; source: DataSource }>()
</script>

<template>
  <article class="kp-metric">
    <span class="kp-metric__label">{{ label }}</span>
    <strong v-if="value !== null && value !== undefined" class="kp-metric__value">{{ typeof value === 'number' ? value.toLocaleString() : value }}</strong>
    <strong v-else class="kp-metric__value kp-metric__value--muted">Not available</strong>
    <span v-if="detail" class="kp-metric__detail">{{ detail }}</span>
    <SourceBadge :source="source" />
  </article>
</template>
