<script setup lang="ts">
import SafeText from './SafeText.vue'

export interface Column { key: string; label: string }
defineProps<{ caption: string; columns: Column[]; rows: Array<Record<string, unknown>>; rowKey?: string; emptyMessage?: string }>()
</script>

<template>
  <div class="kp-table-wrap">
    <table class="kp-table" :aria-label="caption">
      <caption class="kp-visually-hidden">{{ caption }}</caption>
      <thead>
        <tr><th v-for="column in columns" :key="column.key" scope="col">{{ column.label }}</th></tr>
      </thead>
      <tbody>
        <tr v-for="(row, index) in rows" :key="String(row[rowKey || 'id'] ?? index)">
          <td v-for="column in columns" :key="column.key">
            <slot :name="`cell-${column.key}`" :row="row" :value="row[column.key]">
              <SafeText :value="row[column.key]" />
            </slot>
          </td>
        </tr>
        <tr v-if="!rows.length" class="kp-table__empty"><td :colspan="columns.length">{{ emptyMessage || 'No records.' }}</td></tr>
      </tbody>
    </table>
  </div>
</template>
