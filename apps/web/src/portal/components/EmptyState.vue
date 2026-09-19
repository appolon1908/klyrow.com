<script setup lang="ts">
defineProps<{ title: string; description?: string }>()
</script>

<template>
  <div class="kp-state" role="status">
    <h2>{{ title }}</h2>
    <p v-if="description">{{ description }}</p>
    <div v-if="$slots.default" class="kp-state__actions"><slot /></div>
  </div>
</template>
