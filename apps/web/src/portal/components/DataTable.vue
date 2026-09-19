<script setup lang="ts">
import SafeText from './SafeText.vue'

export interface Column { key: string; label: string }
type Row = Record<string, unknown>
// Rows are typed records in pages; the table reads them by column key.
const props = defineProps<{ caption: string; columns: Column[]; rows: readonly object[]; rowKey?: string; emptyMessage?: string }>()
const records = () => props.rows as readonly Row[]
</script>

<template>
  <div class="kp-table-wrap">
    <table class="kp-table" :aria-label="caption">
      <caption class="kp-visually-hidden">{{ caption }}</caption>
      <thead>
        <tr><th v-for="column in columns" :key="column.key" scope="col">{{ column.label }}</th></tr>
      </thead>
      <tbody>
        <tr v-for="(row, index) in records()" :key="String(row[rowKey || 'id'] ?? index)">
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
