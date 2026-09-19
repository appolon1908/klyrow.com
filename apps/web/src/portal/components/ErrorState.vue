<script setup lang="ts">
import { describeFailure, failureTitle, type PortalFailure } from '../errors'
import RequestIdentifiers from './RequestIdentifiers.vue'

defineProps<{ failure: PortalFailure; title?: string }>()
const emit = defineEmits<{ retry: [] }>()
</script>

<template>
  <div class="kp-state kp-state--error" role="alert">
    <h2>{{ title || failureTitle(failure) }}</h2>
    <p>{{ describeFailure(failure) }}</p>
    <RequestIdentifiers :request-id="failure.requestId" :correlation-id="failure.correlationId" :code="failure.code" :status="failure.status" />
    <div class="kp-state__actions">
      <button type="button" class="kp-button" @click="emit('retry')">Try again</button>
      <slot />
    </div>
  </div>
</template>
