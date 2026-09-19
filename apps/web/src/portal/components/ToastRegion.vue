<script setup lang="ts">
import { dismissToast, toasts } from '../toasts'
</script>

<template>
  <div class="kp-toasts" aria-live="polite" aria-atomic="false">
    <div v-for="toast in toasts" :key="toast.id" class="kp-toast" :data-kind="toast.kind" :role="toast.kind === 'error' ? 'alert' : 'status'">
      <div>
        {{ toast.message }}
        <small v-if="toast.requestId">Request ID: {{ toast.requestId }}</small>
      </div>
      <button type="button" class="kp-icon-button" aria-label="Dismiss notification" @click="dismissToast(toast.id)">×</button>
    </div>
  </div>
</template>
