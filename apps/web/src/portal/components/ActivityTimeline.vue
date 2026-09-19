<script setup lang="ts">
import SafeText from './SafeText.vue'

export interface TimelineEntry { id: string; title: string; detail?: string; at?: string | null; tone?: 'success' | 'danger' | 'warning' | '' }
defineProps<{ entries: TimelineEntry[]; emptyMessage?: string }>()
</script>

<template>
  <ol v-if="entries.length" class="kp-timeline">
    <li v-for="entry in entries" :key="entry.id">
      <span class="kp-timeline__dot" :data-tone="entry.tone || ''" aria-hidden="true"></span>
      <div>
        <strong><SafeText :value="entry.title" /></strong>
        <small v-if="entry.detail"><SafeText :value="entry.detail" /></small>
        <time v-if="entry.at" :datetime="entry.at">{{ new Date(entry.at).toLocaleString() }}</time>
      </div>
    </li>
  </ol>
  <p v-else class="kp-muted">{{ emptyMessage || 'No activity recorded.' }}</p>
</template>
