<script setup lang="ts">
withDefaults(defineProps<{ lines?: number }>(), { lines: 3 })
</script>

<template>
  <div class="kp-skeleton" aria-hidden="true">
    <span v-for="index in lines" :key="index" :style="{ width: `${92 - index * 10}%` }"></span>
  </div>
</template>
