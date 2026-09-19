import { reactive } from 'vue'

/**
 * In-memory, tenant-bound page cache. Nothing here ever reaches browser
 * storage; binding a different organization discards every entry so a
 * previous tenant's data cannot survive an organization switch.
 */
const state = reactive({ tenantId: '', entries: new Map<string, unknown>() })

export function bindTenant(tenantId: string): void {
  if (state.tenantId === tenantId) return
  state.entries.clear()
  state.tenantId = tenantId
}

export function boundTenant(): string {
  return state.tenantId
}

export function readTenantState<T>(key: string): T | undefined {
  return state.entries.get(key) as T | undefined
}

export function writeTenantState<T>(key: string, value: T): T {
  state.entries.set(key, value)
  return value
}

export function clearTenantState(): void {
  state.entries.clear()
}
