<script setup lang="ts">
import RequestIdentifiers from './RequestIdentifiers.vue'

defineProps<{ message: string; requestId?: string; code?: string }>()
</script>

<template>
  <div class="kp-notice kp-notice--warning" role="status">
    <strong>Degraded:</strong> {{ message }}
    <RequestIdentifiers :request-id="requestId" :code="code" />
  </div>
</template>
