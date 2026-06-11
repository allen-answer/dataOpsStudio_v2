/**
 * 极简全局 toast —— 模块级单例响应式队列(不引第三方库)。
 * 用于一次性成功 / 失败提示(如 force-logout 后的"已下线"反馈)。
 */
import { ref } from 'vue'

export type ToastKind = 'success' | 'error' | 'info'

export interface ToastItem {
  id: number
  kind: ToastKind
  message: string
}

const toasts = ref<ToastItem[]>([])
let seq = 0

function push(kind: ToastKind, message: string, ttl = 3200): void {
  const id = ++seq
  toasts.value.push({ id, kind, message })
  window.setTimeout(() => dismiss(id), ttl)
}

function dismiss(id: number): void {
  toasts.value = toasts.value.filter((t) => t.id !== id)
}

export function useToast(): {
  toasts: typeof toasts
  success: (m: string) => void
  error: (m: string) => void
  info: (m: string) => void
  dismiss: (id: number) => void
} {
  return {
    toasts,
    success: (m: string) => push('success', m),
    error: (m: string) => push('error', m),
    info: (m: string) => push('info', m),
    dismiss,
  }
}
