import { onMounted, ref, shallowRef, type Ref, type ShallowRef } from 'vue'
import { normalizeFailure, type PortalFailure } from '../errors'
import { readTenantState, writeTenantState } from '../state'

export type PageStatus = 'loading' | 'ready' | 'empty' | 'error' | 'forbidden' | 'session'

export interface PageController<T> {
  status: Ref<PageStatus>
  data: ShallowRef<T | null>
  failure: Ref<PortalFailure | null>
  /** True while showing tenant-cached data that is being refreshed. */
  stale: Ref<boolean>
  reload: () => Promise<void>
}

/**
 * Standard page lifecycle: loading → ready/empty, or forbidden/error with a
 * normalized failure carrying request identifiers. Optional tenant cache keys
 * show the last known tenant-scoped data as `stale` while refreshing.
 */
export function usePage<T>(load: () => Promise<T>, options: { isEmpty?: (data: T) => boolean; cacheKey?: string; immediate?: boolean } = {}): PageController<T> {
  const status = ref<PageStatus>('loading')
  const data = shallowRef<T | null>(null)
  const failure = ref<PortalFailure | null>(null)
  const stale = ref(false)

  async function reload() {
    failure.value = null
    const cached = options.cacheKey ? readTenantState<T>(options.cacheKey) : undefined
    if (cached !== undefined) {
      data.value = cached
      stale.value = true
      status.value = options.isEmpty?.(cached) ? 'empty' : 'ready'
    } else {
      status.value = 'loading'
    }
    try {
      const result = await load()
      data.value = result
      if (options.cacheKey) writeTenantState(options.cacheKey, result)
      status.value = options.isEmpty?.(result) ? 'empty' : 'ready'
    } catch (error) {
      const normalized = normalizeFailure(error)
      failure.value = normalized
      status.value = normalized.kind === 'forbidden' ? 'forbidden' : normalized.kind === 'session' ? 'session' : 'error'
    } finally {
      stale.value = false
    }
  }

  if (options.immediate !== false) onMounted(reload)
  return { status, data, failure, stale, reload }
}

/** Resolve several independent loaders, keeping partial results and per-source failures. */
export async function settle<T extends Record<string, () => Promise<unknown>>>(loaders: T): Promise<{
  [K in keyof T]: { value: Awaited<ReturnType<T[K]>> | null; failure: PortalFailure | null }
}> {
  const keys = Object.keys(loaders) as Array<keyof T>
  const results = await Promise.allSettled(keys.map(key => loaders[key]()))
  const out = {} as { [K in keyof T]: { value: Awaited<ReturnType<T[K]>> | null; failure: PortalFailure | null } }
  keys.forEach((key, index) => {
    const result = results[index]
    out[key] = result.status === 'fulfilled'
      ? { value: result.value as Awaited<ReturnType<T[typeof key]>>, failure: null }
      : { value: null, failure: normalizeFailure(result.reason) }
  })
  return out
}
