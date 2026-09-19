<script setup lang="ts">
withDefaults(defineProps<{ label?: string; lines?: number }>(), { label: 'Loading…', lines: 3 })
</script>

<template>
  <div class="kp-state" role="status" aria-live="polite">
    <p class="kp-muted">{{ label }}</p>
    <div class="kp-skeleton" aria-hidden="true">
      <span v-for="index in lines" :key="index" :style="{ width: `${90 - index * 12}%` }"></span>
    </div>
  </div>
</template>
