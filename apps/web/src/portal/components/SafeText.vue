<script setup lang="ts">
// Renders any user- or provider-supplied value strictly as text (never v-html).
defineProps<{ value: unknown; fallback?: string }>()
</script>

<template>
  <span>{{ value === null || value === undefined || value === '' ? (fallback ?? '—') : String(value) }}</span>
</template>
