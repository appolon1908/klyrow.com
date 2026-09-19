<script setup lang="ts">
export interface Tab { id: string; label: string }
const props = defineProps<{ tabs: Tab[]; modelValue: string; label?: string }>()
const emit = defineEmits<{ 'update:modelValue': [value: string] }>()

function move(event: KeyboardEvent, index: number) {
  const delta = event.key === 'ArrowRight' ? 1 : event.key === 'ArrowLeft' ? -1 : event.key === 'Home' ? -index : event.key === 'End' ? props.tabs.length - 1 - index : 0
  if (!delta) return
  event.preventDefault()
  const next = props.tabs[(index + delta + props.tabs.length) % props.tabs.length]
  emit('update:modelValue', next.id)
  const target = (event.currentTarget as HTMLElement).parentElement?.querySelector<HTMLElement>(`[data-tab="${next.id}"]`)
  target?.focus()
}
</script>

<template>
  <div class="kp-tabs" role="tablist" :aria-label="label || 'Sections'">
    <button
      v-for="(tab, index) in tabs" :id="`tab-${tab.id}`" :key="tab.id" type="button" role="tab" :data-tab="tab.id"
      :aria-selected="tab.id === modelValue ? 'true' : 'false'" :aria-controls="`panel-${tab.id}`" :tabindex="tab.id === modelValue ? 0 : -1"
      @click="emit('update:modelValue', tab.id)" @keydown="move($event, index)"
    >
      {{ tab.label }}
    </button>
  </div>
</template>
