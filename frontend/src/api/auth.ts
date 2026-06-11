import { apiClient } from './client'
import type { TokenResponse } from './types'

/**
 * 登录。mfaCode 可选 —— 仅 MFA 用户在第二步带上(6 位 TOTP 或恢复码)。
 *
 * ★ skipAuthRedirect:登录端点的 401(mfa_required / invalid_mfa_code /
 *   invalid_credentials)是「这次登录没成」,不是「会话过期」。此时根本没 token,
 *   不能触发全局 logout 跳转 —— 由 LoginView 自行分支处理这些 401 code。
 */
export function loginRequest(
  username: string,
  password: string,
  mfaCode?: string,
): Promise<TokenResponse> {
  const body: { username: string; password: string; mfa_code?: string } = {
    username,
    password,
  }
  if (mfaCode) body.mfa_code = mfaCode
  return apiClient.post<TokenResponse>('/auth/login', body, { skipAuthRedirect: true })
}
