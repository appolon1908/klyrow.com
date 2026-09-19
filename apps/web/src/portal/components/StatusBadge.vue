<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ value: string | boolean | null | undefined; tone?: 'success' | 'danger' | 'warning' | 'info' | '' }>()
const SUCCESS = /^(delivered|verified|ready|active|sending_enabled|accepted|completed|paid|healthy|true)$/i
const DANGER = /^(failed|bounced|blocked|suspended|revoked|rejected|error|complained|false)$/i
const WARNING = /^(pending|dns_required|retry|retryable_failure|queued|sending|past_due|grace_period|not_requested|degraded)$/i
const label = computed(() => props.value === true ? 'yes' : props.value === false ? 'no' : String(props.value ?? 'unknown').replace(/_/g, ' ').toLowerCase())
const tone = computed(() => props.tone ?? (SUCCESS.test(String(props.value)) ? 'success' : DANGER.test(String(props.value)) ? 'danger' : WARNING.test(String(props.value)) ? 'warning' : ''))
</script>

<template>
  <span class="kp-badge" :data-tone="tone">{{ label }}</span>
</template>
