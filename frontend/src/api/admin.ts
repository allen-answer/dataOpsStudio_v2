/**
 * Admin API(T7 Part B)—— 严格对齐 app/api/routes/admin.py。
 * 全部端点要求 role=admin(后端 _require_admin → 403),前端守卫 + 403 呈现双层。
 */
import { apiClient } from './client'
import type {
  AdminAiConfigResponse,
  AdminAiConfigTestResponse,
  AdminAiConfigUpdateRequest,
  AdminForceLogoutResponse,
  AdminProjectCreateRequest,
  AdminProjectDeleteImpact,
  AdminProjectItem,
  AdminProjectPatchRequest,
  AdminResetPasswordResponse,
  AdminUserCreateRequest,
  AdminUserItem,
  AuditLogFilters,
  AuditLogItem,
  LicenseStatus,
} from './types'

// ─── License ───────────────────────────────────────────────────────────
/** 已登录用户可读的 license 状态(顶部横条用)。 */
export function getLicenseStatus(): Promise<LicenseStatus> {
  return apiClient.get<LicenseStatus>('/license/status')
}

/** admin 读 license(含 limits)。 */
export function getAdminLicense(): Promise<LicenseStatus> {
  return apiClient.get<LicenseStatus>('/admin/license')
}

/** admin 上传 / 替换 license 文件文本(签名验证后落库)。 */
export function putAdminLicense(licenseText: string): Promise<LicenseStatus> {
  return apiClient.put<LicenseStatus>('/admin/license', { license_text: licenseText })
}

// ─── Users ─────────────────────────────────────────────────────────────
export function listAdminUsers(): Promise<AdminUserItem[]> {
  return apiClient.get<AdminUserItem[]>('/admin/users')
}

export function createAdminUser(req: AdminUserCreateRequest): Promise<AdminUserItem> {
  return apiClient.post<AdminUserItem>('/admin/users', req)
}

/** 当前后端 PATCH 只接受 role(改角色)。 */
export function patchAdminUserRole(userId: string, role: string): Promise<AdminUserItem> {
  return apiClient.patch<AdminUserItem>(`/admin/users/${userId}`, { role })
}

export function resetAdminUserPassword(userId: string): Promise<AdminResetPasswordResponse> {
  return apiClient.post<AdminResetPasswordResponse>(`/admin/users/${userId}/reset-password`)
}

/** 强制下线:吊销该用户当前所有 JWT(revoked_after 之前签发的 token 失效)。 */
export function forceLogoutAdminUser(userId: string): Promise<AdminForceLogoutResponse> {
  return apiClient.post<AdminForceLogoutResponse>(`/admin/users/${userId}/force-logout`)
}

export function disableAdminUserMfa(userId: string): Promise<AdminUserItem> {
  return apiClient.post<AdminUserItem>(`/admin/users/${userId}/mfa/disable`)
}

/**
 * 删除用户。若仍是某些 project 的 owner,后端返 409(error=user_owns_projects,
 * body.owned_projects)。client 把非 2xx 抛 ApiError(body 保留),调用方读 e.body。
 */
export function deleteAdminUser(userId: string): Promise<{ deleted: boolean }> {
  return apiClient.delete<{ deleted: boolean }>(`/admin/users/${userId}`)
}

// ─── Projects ──────────────────────────────────────────────────────────
export function listAdminProjects(): Promise<AdminProjectItem[]> {
  return apiClient.get<AdminProjectItem[]>('/admin/projects')
}

export function createAdminProject(req: AdminProjectCreateRequest): Promise<AdminProjectItem> {
  return apiClient.post<AdminProjectItem>('/admin/projects', req)
}

export function patchAdminProject(
  projectId: string,
  req: AdminProjectPatchRequest,
): Promise<AdminProjectItem> {
  return apiClient.patch<AdminProjectItem>(`/admin/projects/${projectId}`, req)
}

/** 删除前先查级联影响(数据源数 / 任务数),用于二次确认展示。 */
export function getAdminProjectDeleteImpact(
  projectId: string,
): Promise<AdminProjectDeleteImpact> {
  return apiClient.get<AdminProjectDeleteImpact>(`/admin/projects/${projectId}/impact`)
}

export function deleteAdminProject(
  projectId: string,
): Promise<{ deleted: boolean; impact: AdminProjectDeleteImpact }> {
  return apiClient.delete<{ deleted: boolean; impact: AdminProjectDeleteImpact }>(
    `/admin/projects/${projectId}`,
  )
}

// ─── Audit logs ──────────────────────────────────────────────────────────
export function listAdminAuditLogs(filters: AuditLogFilters = {}): Promise<AuditLogItem[]> {
  const params = new URLSearchParams()
  if (filters.start) params.set('start', filters.start)
  if (filters.end) params.set('end', filters.end)
  if (filters.user_id) params.set('user_id', filters.user_id)
  if (filters.action) params.set('action', filters.action)
  if (filters.result) params.set('result', filters.result)
  if (filters.resource_type) params.set('resource_type', filters.resource_type)
  params.set('limit', String(filters.limit ?? 100))
  params.set('offset', String(filters.offset ?? 0))
  return apiClient.get<AuditLogItem[]>(`/admin/audit-logs?${params.toString()}`)
}

// ─── AI 配置(§9)──────────────────────────────────────────────────────────
export function getAdminAiConfig(): Promise<AdminAiConfigResponse> {
  return apiClient.get<AdminAiConfigResponse>('/admin/ai-config')
}

export function putAdminAiConfig(
  req: AdminAiConfigUpdateRequest,
): Promise<AdminAiConfigResponse> {
  return apiClient.put<AdminAiConfigResponse>('/admin/ai-config', req)
}

/** 测试连接 —— 用当前已落库配置发一次 ping。失败返结构化 error(见 type 注释)。 */
export function testAdminAiConfig(): Promise<AdminAiConfigTestResponse> {
  return apiClient.post<AdminAiConfigTestResponse>('/admin/ai-config/test')
}
