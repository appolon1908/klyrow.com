<script setup lang="ts">
defineProps<{ title: string; dependency: string; alternatives?: Array<{ label: string; href: string }> }>()
</script>

<template>
  <div class="kp-state kp-state--info" role="status">
    <p class="kp-eyebrow">Capability check</p>
    <h2>{{ title }} is not available in this release</h2>
    <p>This page is capability-aware: it only reports as operational once the required browser API exists and has been verified. No placeholder or sample data is displayed.</p>
    <div class="kp-contract">{{ dependency }}</div>
    <div v-if="alternatives?.length" class="kp-state__actions">
      <a v-for="item in alternatives" :key="item.href" class="kp-button" :href="item.href">{{ item.label }}</a>
    </div>
    <slot />
  </div>
</template>
