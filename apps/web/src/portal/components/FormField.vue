<script setup lang="ts">
defineProps<{ id: string; label: string; hint?: string; error?: string }>()
</script>

<template>
  <div class="kp-field">
    <label :for="id">{{ label }}</label>
    <slot />
    <span v-if="hint" :id="`${id}-hint`" class="kp-field__hint">{{ hint }}</span>
    <span v-if="error" :id="`${id}-error`" class="kp-field__error" role="alert">{{ error }}</span>
  </div>
</template>
