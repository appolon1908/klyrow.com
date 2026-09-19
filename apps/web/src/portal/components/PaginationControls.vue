<script setup lang="ts">
defineProps<{ offset: number; limit: number; count: number; hasMore: boolean }>()
const emit = defineEmits<{ previous: []; next: [] }>()
</script>

<template>
  <nav class="kp-pagination" aria-label="Pagination">
    <span>{{ count ? `${offset + 1}–${offset + count}` : 'No rows' }}</span>
    <button type="button" class="kp-button" :disabled="offset === 0" @click="emit('previous')">Previous</button>
    <button type="button" class="kp-button" :disabled="!hasMore" @click="emit('next')">Next</button>
  </nav>
</template>
