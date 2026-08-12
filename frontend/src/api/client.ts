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
export interface RequestOptions {
  skipAuthRedirect?: boolean
  signal?: AbortSignal
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
      signal: options?.signal,
    })
  } catch (networkError) {
    if (networkError instanceof DOMException && networkError.name === 'AbortError') {
      throw networkError
    }
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
      retryAfterMs(response.headers.get('Retry-After')),
    )
  }

  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

/**
 * postRaw:鉴权二进制上传(POST /projects/{pid}/uploads?purpose=&filename=)。
 * 与 request() 的区别仅在于 body 直接透传 Blob/File 不做 JSON.stringify,
 * Content-Type 用文件真实类型;401 / 非 2xx 错误处理与 request() 一致。
 */
async function postRaw<T>(path: string, body: Blob, contentType?: string): Promise<T> {
  const headers: Record<string, string> = {
    'Content-Type': contentType || 'application/octet-stream',
  }
  const token = getToken()
  if (token) headers.Authorization = `Bearer ${token}`

  let response: Response
  try {
    response = await fetch(`/api${path}`, { method: 'POST', headers, body })
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
  return response.json() as Promise<T>
}

/**
 * downloadBlob:鉴权二进制下载(GET /exports/{token})。
 * 用 fetch 带 Bearer token 取 blob,顺便从 Content-Disposition 解析文件名。
 * 非 2xx 复用 ApiError(status + code) —— 410 download_token_consumed/expired
 * 由调用方分支提示「重新导出」。
 */
async function downloadBlob(
  path: string,
): Promise<{ blob: Blob; filename: string | null }> {
  const headers: Record<string, string> = {}
  const token = getToken()
  if (token) headers.Authorization = `Bearer ${token}`

  let response: Response
  try {
    response = await fetch(`/api${path}`, { method: 'GET', headers })
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

  const blob = await response.blob()
  const disposition = response.headers.get('Content-Disposition') ?? ''
  const match = /filename="?([^"]+)"?/i.exec(disposition)
  return { blob, filename: match ? match[1] : null }
}

/**
 * downloadTokenizedExport —— 共享的一次性导出下载:GET /exports/{token} 鉴权取 blob
 * 后触发浏览器下载。compare.ts downloadCompareExport / metadata.ts downloadExport
 * 复用此实现(#96 review #4:两处逐字重复,提取共享 helper)。
 * token 一次性:用过/过期后端返 410,调用方据 ApiError.status===410 提示「重新导出」。
 */
export async function downloadTokenizedExport(
  token: string,
  fallbackFilename: string,
): Promise<void> {
  const { blob, filename } = await downloadBlob(`/exports/${encodeURIComponent(token)}`)
  const url = URL.createObjectURL(blob)
  try {
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = filename ?? fallbackFilename
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
  } finally {
    URL.revokeObjectURL(url)
  }
}

export const apiClient = {
  get: <T>(path: string, options?: RequestOptions): Promise<T> =>
    request<T>('GET', path, undefined, options),
  downloadBlob,
  postRaw,
  post: <T>(path: string, body?: unknown, options?: RequestOptions): Promise<T> =>
    request<T>('POST', path, body, options),
  put: <T>(path: string, body?: unknown, options?: RequestOptions): Promise<T> =>
    request<T>('PUT', path, body, options),
  patch: <T>(path: string, body?: unknown): Promise<T> => request<T>('PATCH', path, body),
  delete: <T>(path: string): Promise<T> => request<T>('DELETE', path),
}

function retryAfterMs(value: string | null): number | undefined {
  if (!value) return undefined
  const seconds = Number(value)
  if (Number.isFinite(seconds) && seconds >= 0) return Math.ceil(seconds * 1000)
  const deadline = Date.parse(value)
  if (!Number.isFinite(deadline)) return undefined
  return Math.max(0, deadline - Date.now())
}
