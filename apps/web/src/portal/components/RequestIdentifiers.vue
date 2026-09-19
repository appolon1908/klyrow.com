<script setup lang="ts">
defineProps<{ requestId?: string; correlationId?: string; code?: string; status?: number }>()
</script>

<template>
  <div v-if="requestId || correlationId || code" class="kp-identifiers">
    <span v-if="code"><strong>Code:</strong> {{ code }}<template v-if="status"> (HTTP {{ status }})</template></span>
    <span v-if="requestId"><strong>Request ID:</strong> {{ requestId }}</span>
    <span v-if="correlationId"><strong>Correlation ID:</strong> {{ correlationId }}</span>
    <span v-if="!requestId && !correlationId" class="kp-muted">No request identifier was returned.</span>
  </div>
</template>
