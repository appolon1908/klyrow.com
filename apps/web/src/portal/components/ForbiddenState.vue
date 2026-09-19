<script setup lang="ts">
import RequestIdentifiers from './RequestIdentifiers.vue'

withDefaults(defineProps<{ reason?: 'capability' | 'role' | 'platform-admin' | 'server'; requestId?: string; code?: string }>(), { reason: 'capability', requestId: '', code: '' })
const messages = {
  capability: 'Your current organization access does not include the capability this page needs.',
  role: 'This page needs an owner or admin role in the current organization.',
  'platform-admin': 'This area is restricted to verified platform administrators.',
  server: 'The server declined this request for your current session.',
}
</script>

<template>
  <div class="kp-state kp-state--warning" role="alert">
    <h2>Access denied</h2>
    <p>{{ messages[reason] }} Access is always enforced by the server; contact an organization owner if you need this capability.</p>
    <RequestIdentifiers :request-id="requestId" :code="code" />
    <div class="kp-state__actions">
      <a class="kp-button" href="/app/overview">Back to overview</a>
      <slot />
    </div>
  </div>
</template>
