<script setup lang="ts">
import PageHeader from '../components/PageHeader.vue'
import EmptyState from '../components/EmptyState.vue'
defineProps<{ path: string }>()
</script>

<template>
  <div>
    <PageHeader title="Page not found" />
    <EmptyState title="There is no portal page at this address" description="Check the address or use the navigation to find the page you need.">
      <a class="kp-button" href="/app/overview">Go to overview</a>
    </EmptyState>
  </div>
</template>
