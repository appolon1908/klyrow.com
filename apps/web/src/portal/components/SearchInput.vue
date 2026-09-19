<script setup lang="ts">
defineProps<{ modelValue: string; label: string; placeholder?: string; id?: string }>()
const emit = defineEmits<{ 'update:modelValue': [value: string] }>()
</script>

<template>
  <div class="kp-field">
    <label :for="id || 'kp-search-input'">{{ label }}</label>
    <input :id="id || 'kp-search-input'" type="search" :value="modelValue" :placeholder="placeholder" autocomplete="off" @input="emit('update:modelValue', ($event.target as HTMLInputElement).value)">
  </div>
</template>
