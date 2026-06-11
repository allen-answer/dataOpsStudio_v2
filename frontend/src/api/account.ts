/**
 * 账户安全 API(Wave 4B)—— 严格对齐 app/api/routes/account.py。
 *
 * ★ 改密 / 关 MFA / 重生成恢复码的 401 是「旧密码 / 当前 TOTP 不对」的业务校验失败,
 *   不是会话失效 —— 全部带 skipAuthRedirect,避免被全局 401 handler 踢下线。
 *   调用方自己读 ApiError.code(invalid_password / invalid_mfa_code)分支提示。
 */
import { apiClient } from './client'
import type {
  AccountSecurityStatus,
  MfaEnrollResponse,
  MfaVerifyResponse,
  RecoveryCodesResponse,
} from './types'

export function getAccountSecurity(): Promise<AccountSecurityStatus> {
  return apiClient.get<AccountSecurityStatus>('/account/security')
}

/** 改密码。401 invalid_password = 旧密码错(不踢下线)。 */
export function changeAccountPassword(
  oldPassword: string,
  newPassword: string,
): Promise<{ changed: boolean }> {
  return apiClient.post<{ changed: boolean }>(
    '/account/password',
    { old_password: oldPassword, new_password: newPassword },
    { skipAuthRedirect: true },
  )
}

/** 开始 enroll → 返 secret + otpauth URI(前端转 QR)。409 mfa_already_enabled。 */
export function enrollMfa(): Promise<MfaEnrollResponse> {
  return apiClient.post<MfaEnrollResponse>('/account/mfa/enroll')
}

/** 验证 enroll 的 6 位码 → 开启成功,一次性回 8 个恢复码。401 invalid_mfa_code(不踢下线)。 */
export function verifyMfa(code: string): Promise<MfaVerifyResponse> {
  return apiClient.post<MfaVerifyResponse>(
    '/account/mfa/verify',
    { code },
    { skipAuthRedirect: true },
  )
}

/** 关闭 MFA(需当前 6 位 TOTP)。401 invalid_mfa_code(不踢下线)。 */
export function disableMfa(code: string): Promise<{ enabled: boolean }> {
  return apiClient.post<{ enabled: boolean }>(
    '/account/mfa/disable',
    { code },
    { skipAuthRedirect: true },
  )
}

/** 重新生成恢复码(需当前 6 位 TOTP,旧码全部失效)。401 invalid_mfa_code(不踢下线)。 */
export function regenerateRecoveryCodes(code: string): Promise<RecoveryCodesResponse> {
  return apiClient.post<RecoveryCodesResponse>(
    '/account/recovery-codes/regenerate',
    { code },
    { skipAuthRedirect: true },
  )
}
