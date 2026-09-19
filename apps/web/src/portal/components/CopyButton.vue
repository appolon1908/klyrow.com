<script setup lang="ts">
import { onBeforeUnmount, ref } from 'vue'

// The value lives only in component memory. With `sensitive`, it is never
// rendered into the DOM and no storage API is ever used.
const props = withDefaults(defineProps<{ value: string; label?: string; sensitive?: boolean }>(), { label: 'Copy', sensitive: false })
const status = ref('')
let timer = 0
async function copy() {
  try {
    await navigator.clipboard.writeText(props.value)
    status.value = 'Copied to clipboard'
  } catch {
    status.value = 'Copy is not available in this browser'
  }
  window.clearTimeout(timer)
  timer = window.setTimeout(() => { status.value = '' }, 2500)
}
onBeforeUnmount(() => window.clearTimeout(timer))
</script>

<template>
  <span class="kp-inline-actions">
    <button type="button" class="kp-button" @click="copy">{{ label }}</button>
    <span role="status" aria-live="polite" class="kp-muted">{{ status }}</span>
  </span>
</template>
