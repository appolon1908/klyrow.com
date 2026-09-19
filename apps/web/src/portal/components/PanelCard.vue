<script setup lang="ts">
import SourceBadge, { type DataSource } from './SourceBadge.vue'

defineProps<{ title?: string; eyebrow?: string; source?: DataSource }>()
</script>

<template>
  <section class="kp-panel">
    <div v-if="title || $slots.actions" class="kp-panel__title">
      <div>
        <p v-if="eyebrow" class="kp-eyebrow">{{ eyebrow }}</p>
        <h2 v-if="title">{{ title }}</h2>
      </div>
      <div class="kp-inline-actions">
        <SourceBadge v-if="source" :source="source" />
        <slot name="actions" />
      </div>
    </div>
    <slot />
  </section>
</template>
