/**
 * 与后端 schema 对齐的类型(契约 §5 + 后续路由扩展)。
 *
 * 后端 schema 改了这里要同步;若漂移,build 时 vue-tsc 严格类型会报错。
 */

export interface TokenResponse {
  access_token: string
  token_type: string
}

export interface User {
  id: string
  role: 'admin' | 'member' | string
}

export interface Project {
  id: string
  name: string
  description: string | null
}

export type DbType = 'mysql' | 'oracle' | 'dm' | 'db2' | 'postgresql'

export interface DatasourceListItem {
  id: string
  name: string
  db_type: DbType
  host: string
  port: number
  environment: string
  environment_verified: boolean
  database: string | null
  created_at: string
}

export interface DatasourceCreateRequest {
  project_id: string
  name: string
  db_type: DbType
  host: string
  port: number
  username: string
  database: string
  password: string
  environment?: string
  extra?: Record<string, unknown>
}

// 详情 / 建后端点(GET /datasources/{id}, POST /datasources)返回体不含 environment_verified
// —— 该字段只在列表端点(GET /datasources)产出。用 Omit 精确对齐后端,避免凭空多出字段。
export interface DatasourceResponse extends Omit<DatasourceListItem, 'environment_verified'> {
  project_id: string
  username: string
  extra: Record<string, unknown>
}

export type JobStatus = 'pending' | 'running' | 'success' | 'failed' | 'cancelled' | 'timeout'

export interface JobListItem {
  id: string
  kind: string
  status: JobStatus
  created_at: string
  started_at: string | null
  finished_at: string | null
  error: string | null
}

export interface SqlExecuteResponse {
  job_id: string
  result_set_id: string
}

export interface Column {
  name: string
  type: string
  nullable: boolean
  primary_key: boolean
}

export interface RowResponse {
  values: unknown[]
}

export interface JobResultResponse {
  job_id: string
  result_set_id: string
  offset: number
  limit: number
  columns: Column[]
  rows: RowResponse[]
  loaded_rows: number | null
  truncated: boolean | null
}

// ─── Admin / License(T7 Part B,严格对齐 app/api/schemas.py)─────────────

/** License 5 态 —— 后端 mode 是自由字符串,枚举锚定在前端。 */
export type LicenseMode = 'valid' | 'trial' | 'in_grace' | 'expired' | 'repair' | string

/** GET /api/license/status(已登录可访问)+ GET/PUT /api/admin/license(admin)。 */
export interface LicenseStatus {
  mode: LicenseMode
  edition: string | null
  customer: string | null
  expires_at: string | null
  limits: Record<string, unknown>
  features: string[]
  repair_reason: string | null
  trial_days_remaining: number | null
}

/** GET /api/admin/users → AdminUserItem。 */
export interface AdminUserItem {
  id: string
  username: string
  role: string
  mfa_enabled: boolean
  created_at: string
}

export interface AdminUserCreateRequest {
  username: string
  role: string
  initial_password: string
}

export interface AdminResetPasswordResponse {
  temporary_password: string
}

/** DELETE /api/admin/users/{id} 409 体(user 仍是某些 project 的 owner)。 */
export interface AdminUserDeleteBlocked {
  error: string
  message: string
  owned_projects: Project[]
}

/** GET /api/admin/projects → AdminProjectItem(含计数)。 */
export interface AdminProjectItem {
  id: string
  name: string
  description: string | null
  owner_user_id: string
  member_count: number
  datasource_count: number
  job_count: number
  created_at: string
}

export interface AdminProjectCreateRequest {
  name: string
  description?: string | null
  owner_user_id: string
  members?: string[]
}

export interface AdminProjectPatchRequest {
  name?: string | null
  description?: string | null
  owner_user_id?: string | null
  members?: string[] | null
}

/** GET /api/admin/projects/{id}/impact + DELETE 响应里的 impact 块。 */
export interface AdminProjectDeleteImpact {
  datasource_count: number
  job_count: number
}

/** GET /api/admin/audit-logs → AuditLogItem。 */
export interface AuditLogItem {
  id: number
  ts: string
  user_id: string | null
  project_id: string | null
  action: string
  resource_type: string | null
  resource_id: string | null
  result: string
  request_id: string | null
  detail: Record<string, unknown> | null
}

export interface AuditLogFilters {
  start?: string
  end?: string
  user_id?: string
  action?: string
  result?: string
  resource_type?: string
  limit?: number
  offset?: number
}

/** 服务端错误响应统一形态(由 ApiError handler 产出)。 */
export interface ApiErrorBody {
  error?: string
  message?: string
  detail?: unknown
}

export class ApiError extends Error {
  status: number
  code?: string
  body?: ApiErrorBody

  constructor(status: number, message: string, code?: string, body?: ApiErrorBody) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.body = body
  }
}
