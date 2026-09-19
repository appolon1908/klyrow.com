<script setup lang="ts">
defineProps<{ items: Array<{ label: string; href?: string }> }>()
</script>

<template>
  <nav class="kp-breadcrumbs" aria-label="Breadcrumb">
    <ol>
      <li v-for="(item, index) in items" :key="index">
        <a v-if="item.href && index < items.length - 1" :href="item.href">{{ item.label }}</a>
        <span v-else :aria-current="index === items.length - 1 ? 'page' : undefined">{{ item.label }}</span>
      </li>
    </ol>
  </nav>
</template>
