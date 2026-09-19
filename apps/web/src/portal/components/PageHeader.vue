<script setup lang="ts">
defineProps<{ title: string; description?: string; eyebrow?: string }>()
</script>

<template>
  <div class="kp-page-header">
    <div>
      <p v-if="eyebrow" class="kp-eyebrow">{{ eyebrow }}</p>
      <h1>{{ title }}</h1>
      <p v-if="description">{{ description }}</p>
    </div>
    <div v-if="$slots.default" class="kp-page-header__actions"><slot /></div>
  </div>
</template>
