import { reactive } from 'vue'

export type ToastKind = 'success' | 'info' | 'warning' | 'error'
export interface Toast { id: number; kind: ToastKind; message: string; requestId?: string }

export const toasts = reactive<Toast[]>([])
let counter = 0

export function notify(kind: ToastKind, message: string, requestId?: string): number {
  const id = ++counter
  toasts.push({ id, kind, message, requestId })
  if (kind !== 'error' && typeof setTimeout === 'function') setTimeout(() => dismissToast(id), 6000)
  return id
}

export function dismissToast(id: number): void {
  const index = toasts.findIndex(toast => toast.id === id)
  if (index >= 0) toasts.splice(index, 1)
}
