<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'

const props = withDefaults(defineProps<{ open: boolean; title: string; role?: 'dialog' | 'alertdialog'; describedBy?: string }>(), { role: 'dialog', describedBy: undefined })
const emit = defineEmits<{ close: [] }>()
const surface = ref<HTMLElement | null>(null)
let opener: HTMLElement | null = null
const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

function focusables(): HTMLElement[] {
  return Array.from(surface.value?.querySelectorAll<HTMLElement>(FOCUSABLE) || [])
}
function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') { event.preventDefault(); emit('close'); return }
  if (event.key !== 'Tab') return
  const items = focusables()
  if (!items.length) { event.preventDefault(); surface.value?.focus(); return }
  const first = items[0], last = items[items.length - 1]
  const active = document.activeElement as HTMLElement | null
  if (event.shiftKey && (active === first || !surface.value?.contains(active))) { event.preventDefault(); last.focus() }
  else if (!event.shiftKey && (active === last || !surface.value?.contains(active))) { event.preventDefault(); first.focus() }
}
watch(() => props.open, async open => {
  if (open) {
    opener = document.activeElement as HTMLElement | null
    await nextTick()
    const initial = focusables().find(item => !item.closest('.kp-dialog__header')) || focusables()[0] || surface.value
    initial?.focus()
  } else {
    opener?.focus()
    opener = null
  }
}, { immediate: true })
onBeforeUnmount(() => opener?.focus())
</script>

<template>
  <div v-if="open" class="kp-dialog-layer" @click.self="emit('close')">
    <div ref="surface" class="kp-dialog" :role="role" aria-modal="true" aria-labelledby="kp-dialog-title" :aria-describedby="describedBy" tabindex="-1" @keydown="onKeydown">
      <header class="kp-dialog__header">
        <h2 id="kp-dialog-title">{{ title }}</h2>
        <button type="button" class="kp-icon-button" aria-label="Close dialog" @click="emit('close')">×</button>
      </header>
      <div class="kp-dialog__body"><slot /></div>
      <footer v-if="$slots.footer" class="kp-dialog__footer"><slot name="footer" /></footer>
    </div>
  </div>
</template>
