<script setup lang="ts">
import ModalDialog from './ModalDialog.vue'

withDefaults(defineProps<{ open: boolean; title: string; description: string; confirmLabel?: string; destructive?: boolean; busy?: boolean }>(), { confirmLabel: 'Confirm', destructive: false, busy: false })
const emit = defineEmits<{ confirm: []; cancel: [] }>()
</script>

<template>
  <ModalDialog :open="open" :title="title" role="alertdialog" described-by="kp-confirm-description" @close="emit('cancel')">
    <p id="kp-confirm-description">{{ description }}</p>
    <slot />
    <template #footer>
      <button type="button" class="kp-button" :disabled="busy" @click="emit('cancel')">Cancel</button>
      <button type="button" :class="destructive ? 'kp-button--danger' : 'kp-button--primary'" :disabled="busy" @click="emit('confirm')">{{ busy ? 'Working…' : confirmLabel }}</button>
    </template>
  </ModalDialog>
</template>
