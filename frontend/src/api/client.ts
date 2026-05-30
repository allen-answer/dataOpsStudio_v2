/**
 * Fetch wrapper:
 *  - 自动从 auth store 取 token 加 Authorization header
 *  - 401 → 清 token + 跳 /login(by router push event)
 *  - 非 2xx → 抛 ApiError(带 status + code + 安全 message)
 *  - 永远走相对路径 /api/...,由 vite dev proxy 转发
 */
import { ApiError, type ApiErrorBody } from './types'

/**
 * onUnauthenticated:401 时触发的回调。
 * router/guards.ts 会在启动时注册一次,把 auth.logout + router.push('/login') 接上去。
 * 这样 api 层不依赖 router/store 具体实现,只发事件。
 */
let onUnauthenticated: () => void = () => {}
export function setUnauthenticatedHandler(fn: () => void): void {
  onUnauthenticated = fn
}

/**
 * getToken:每次请求时由 auth store 提供。在 main.ts 启动时注入。
 */
let getToken: () => string | null = () => null
export function setTokenProvider(fn: () => string | null): void {
  getToken = fn
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  const token = getToken()
  if (token) headers.Authorization = `Bearer ${token}`

  let response: Response
  try {
    response = await fetch(`/api${path}`, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    })
  } catch (networkError) {
    throw new ApiError(
      0,
      networkError instanceof Error ? networkError.message : 'Network error',
      'network_error',
    )
  }

  if (response.status === 401) {
    onUnauthenticated()
    throw new ApiError(401, 'Session expired or invalid', 'unauthenticated')
  }

  if (!response.ok) {
    const errorBody: ApiErrorBody = await response.json().catch(() => ({}))
    throw new ApiError(
      response.status,
      errorBody.message ?? errorBody.error ?? response.statusText,
      errorBody.error,
      errorBody,
    )
  }

  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export const apiClient = {
  get: <T>(path: string): Promise<T> => request<T>('GET', path),
  post: <T>(path: string, body?: unknown): Promise<T> => request<T>('POST', path, body),
  put: <T>(path: string, body?: unknown): Promise<T> => request<T>('PUT', path, body),
  delete: <T>(path: string): Promise<T> => request<T>('DELETE', path),
}
