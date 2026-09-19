<script setup lang="ts">
import { onBeforeUnmount, ref } from 'vue'
import CopyButton from './CopyButton.vue'

// One-time credential display. The value is kept in a local ref, masked by
// default, cleared on close/unmount and never written to storage or URLs.
const props = defineProps<{ label: string; value: string; instructions?: string }>()
const emit = defineEmits<{ done: [] }>()
const secret = ref(props.value)
const revealed = ref(false)
function finish() {
  secret.value = ''
  revealed.value = false
  emit('done')
}
onBeforeUnmount(() => { secret.value = '' })
</script>

<template>
  <div v-if="secret" class="kp-secret" role="region" :aria-label="label">
    <strong>{{ label }} — shown once</strong>
    <p class="kp-muted">{{ instructions || 'Store this value in your secret manager now. Klyrow cannot display it again; rotate it to obtain a new one.' }}</p>
    <div class="kp-secret__value" :class="{ 'kp-secret__masked': !revealed }" aria-live="off">{{ revealed ? secret : '•'.repeat(24) }}</div>
    <div class="kp-inline-actions">
      <button type="button" class="kp-button" @click="revealed = !revealed">{{ revealed ? 'Hide' : 'Reveal' }}</button>
      <CopyButton :value="secret" label="Copy value" sensitive />
      <button type="button" class="kp-button--primary" @click="finish">I have stored it</button>
    </div>
  </div>
</template>
