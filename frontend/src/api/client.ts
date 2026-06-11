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
 * 真会话失效的 401 错误码(后端 auth 中间件 / 依赖:app/api/dependencies.py +
 * middleware/crosscutting.py 均返 error="unauthorized")。
 * skipAuthRedirect 端点(改密 / 关 MFA / 重生成恢复码 / 登录二步)的业务 401
 * 是 invalid_password / invalid_mfa_code / mfa_required / invalid_credentials,
 * 不在此集合 —— 那些不踢下线。
 */
const SESSION_EXPIRED_CODES = new Set(['unauthorized', 'unauthenticated'])

/**
 * isSessionExpired:skipAuthRedirect 调用方在 catch 里判断「这个 401 到底是
 * 真会话过期还是业务校验失败」。真过期 → 调 triggerUnauthenticated() 主动登出,
 * 而非落通用错误文案(#42 review nit)。
 */
export function isSessionExpired(err: unknown): boolean {
  return err instanceof ApiError && err.status === 401 && SESSION_EXPIRED_CODES.has(err.code ?? '')
}

/**
 * triggerUnauthenticated:手动触发全局 logout + 跳 /login,与 request() 内
 * 非 skipAuthRedirect 路径走同一回调(router/guards.ts 注册)。
 */
export function triggerUnauthenticated(): void {
  onUnauthenticated()
}

/**
 * getToken:每次请求时由 auth store 提供。在 main.ts 启动时注入。
 */
let getToken: () => string | null = () => null
export function setTokenProvider(fn: () => string | null): void {
  getToken = fn
}

/**
 * RequestOptions.skipAuthRedirect:把这次请求的 401 当成「业务校验失败」而非
 * 「会话失效」,不触发全局 logout + 跳 /login,只抛 ApiError 给调用方自己分支。
 *
 * 用于两类带 401 语义的端点:
 *   - 登录第二因子:POST /auth/login 401 mfa_required / invalid_mfa_code(此时根本没登录)
 *   - 账户安全二次确认:改密 401 invalid_password、关 MFA / 重生成恢复码 401 invalid_mfa_code
 *     —— 这些 401 是「当前 TOTP / 旧密码不对」,不该把用户踢下线。
 */
interface RequestOptions {
  skipAuthRedirect?: boolean
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  options?: RequestOptions,
): Promise<T> {
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

  if (response.status === 401 && !options?.skipAuthRedirect) {
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
  post: <T>(path: string, body?: unknown, options?: RequestOptions): Promise<T> =>
    request<T>('POST', path, body, options),
  put: <T>(path: string, body?: unknown, options?: RequestOptions): Promise<T> =>
    request<T>('PUT', path, body, options),
  patch: <T>(path: string, body?: unknown): Promise<T> => request<T>('PATCH', path, body),
  delete: <T>(path: string): Promise<T> => request<T>('DELETE', path),
}
