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

/**
 * Datasource operation policy(8 个 allow_* 开关)。
 * 源:app/domain/datasource.py OperationPolicy。默认仅 allow_select=true,其余 false。
 * 2.0.0 仅 allow_select / allow_explain 真生效,其余先落库供 2.1+ 复用。
 */
export interface OperationPolicy {
  allow_select: boolean
  allow_explain: boolean
  allow_dm_explain: boolean
  allow_oracle_plan_table: boolean
  allow_schema_import: boolean
  allow_schema_save: boolean
  allow_scenario_write: boolean
  allow_record_task: boolean
}

/** 后端默认值(app/domain/datasource.py):仅 allow_select 开。 */
export const DEFAULT_OPERATION_POLICY: OperationPolicy = {
  allow_select: true,
  allow_explain: false,
  allow_dm_explain: false,
  allow_oracle_plan_table: false,
  allow_schema_import: false,
  allow_schema_save: false,
  allow_scenario_write: false,
  allow_record_task: false,
}

export interface DatasourceListItem {
  id: string
  name: string
  db_type: DbType
  host: string
  port: number
  environment: string
  environment_verified: boolean
  database: string | null
  operation_policy: OperationPolicy
  created_at: string
}

export interface DatasourceCreateRequest {
  project_id: string
  name: string
  db_type: DbType
  host: string
  port: number
  username: string
  database: string | null
  password: string
  environment?: string
  extra?: Record<string, unknown>
  operation_policy?: OperationPolicy
}

/**
 * PUT /datasources/{id} —— 全字段可选;省略的字段不改。
 * password 缺省或空字符串 = 不修改密码(后端 `if body.password:` 判定)。
 * 源:app/api/schemas.py DatasourceUpdateRequest。
 */
export interface DatasourceUpdateRequest {
  project_id?: string
  name?: string
  db_type?: DbType
  host?: string
  port?: number
  username?: string
  database?: string | null
  password?: string
  environment?: string
  extra?: Record<string, unknown>
  operation_policy?: OperationPolicy
}

// 详情 / 建后端点(GET /datasources/{id}, POST /datasources)返回体不含 environment_verified
// —— 该字段只在列表端点(GET /datasources)产出。用 Omit 精确对齐后端,避免凭空多出字段。
export interface DatasourceResponse
  extends Omit<DatasourceListItem, 'environment_verified'> {
  project_id: string
  username: string
  extra: Record<string, unknown>
}

/** DELETE /datasources/{id} 409 体的引用项。源:DatasourceReferenceItem。 */
export interface DatasourceReferenceItem {
  job_id: string
  kind: string
  status: JobStatus
}

/** DELETE /datasources/{id} 409 体(被既有 job 引用)。源:DatasourceDeleteBlockedResponse。 */
export interface DatasourceDeleteBlocked {
  error: string
  message: string
  references: DatasourceReferenceItem[]
}

/**
 * 测连失败分类码。源:app/api/schemas.py DatasourceTestErrorCode。
 * 2.0.0 实际只稳定产出 unknown / timeout;其余为多方言统一留位。
 */
export type DatasourceTestErrorCode =
  | 'auth_failed'
  | 'host_unreachable'
  | 'timeout'
  | 'permission_denied'
  | 'unknown'

/** POST /datasources/{id}/test 响应。源:DatasourceTestResponse。 */
export interface DatasourceTestResponse {
  ok: boolean
  server_version: string | null
  latency_ms: number | null
  error_code: DatasourceTestErrorCode | null
}

export type JobStatus = 'pending' | 'running' | 'success' | 'failed' | 'cancelled' | 'timeout'

/**
 * 结构化 job 错误码(7 值)。源:app/domain/job.py JobErrorCode。
 * 旧 job(error_code 为 null)回落 JobListItem.error 里的粗分类字符串。
 */
export type JobErrorCode =
  | 'connection_failed'
  | 'sql_failed'
  | 'timeout'
  | 'cancelled'
  | 'permission_denied'
  | 'unsupported_db_type'
  | 'internal'

export interface JobListItem {
  id: string
  kind: string
  status: JobStatus
  created_at: string
  started_at: string | null
  finished_at: string | null
  error: string | null
  error_code: JobErrorCode | null
}

export interface SqlExecuteResponse {
  job_id: string
  result_set_id: string
}

/**
 * 跨方言统一列类型(契约 §3.2 ColumnType,11 值枚举)。
 * 各 adapter 把 driver 类型码映射到这套统一值,前端按此一套语义染色 / 对齐 / 标记,
 * 不再做方言条件分支。无法识别落 'unknown'。源:app/domain/schema.py ColumnType。
 */
export type ColumnType =
  | 'string'
  | 'integer'
  | 'float'
  | 'decimal'
  | 'boolean'
  | 'datetime'
  | 'date'
  | 'time'
  | 'bytes'
  | 'json'
  | 'unknown'

export interface Column {
  name: string
  type: ColumnType // 统一枚举(breaking change:原为 driver 原始字符串)
  driver_type: string | null // 原始 driver 类型(如 "VARCHAR(64)" / "NUMBER(10,2)"),tooltip 展示
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
  total_rows: number | null
  state: string | null
  truncated: boolean | null
}

/** W2 导出格式 —— 源:app/api/schemas.py ExportFormat = Literal["csv","excel","json","sql"]。 */
export type ExportFormat = 'csv' | 'excel' | 'json' | 'sql'

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
  license_enforcement_enabled: boolean
}

export interface SystemSettingsResponse {
  access_token_ttl_seconds: number
  license_enforcement_enabled: boolean
}

export interface SystemSettingsUpdateRequest {
  access_token_ttl_seconds: number
  license_enforcement_enabled: boolean
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

// ─── 账户安全 / MFA(Wave 4B,严格对齐 app/api/routes/account.py + schemas.py)──

/** GET /api/account/security → AccountSecurityStatusResponse。 */
export interface AccountSecurityStatus {
  mfa_enabled: boolean
  recovery_codes_total: number
  recovery_codes_used: number
}

/** POST /api/account/mfa/enroll → MfaEnrollResponse(secret 文本 + otpauth URI 转 QR)。 */
export interface MfaEnrollResponse {
  secret: string
  otpauth_uri: string
}

/** POST /api/account/mfa/verify → MfaVerifyResponse(开启成功,一次性回 8 个恢复码)。 */
export interface MfaVerifyResponse {
  enabled: boolean
  recovery_codes: string[]
}

/** POST /api/account/recovery-codes/regenerate → RecoveryCodesResponse。 */
export interface RecoveryCodesResponse {
  recovery_codes: string[]
}

// ─── AI 配置(Wave 4B,严格对齐 app/api/routes/admin.py ai-config + schemas.py)──

/** Ai provider 枚举。源:app/api/schemas.py AiProvider。 */
export type AiProvider = 'off' | 'mock' | 'openai_compatible' | 'anthropic' | 'ollama'

/** API key 来源标记。源:AiApiKeySource。 */
export type AiApiKeySource = 'none' | 'stored' | 'env'

/** GET /api/admin/ai-config → AdminAiConfigResponse(key 永不回显,只给 source / has_stored)。 */
export interface AdminAiConfigResponse {
  enabled: boolean
  provider: AiProvider
  model: string | null
  base_url: string | null
  max_auto_egress_level: number // 0..3(后端 le=3;L4 永不可经此设置)
  l4_requires_optin: boolean // 后端恒 true,前端只读展示锁定
  enable_inference: boolean
  enable_auto_translation: boolean
  api_key_source: AiApiKeySource
  has_stored_api_key: boolean
  updated_at: string | null
}

/**
 * PUT /api/admin/ai-config 请求体。源:AdminAiConfigUpdateRequest。
 * ★ api_key 与 clear_api_key 互斥(后端 400 invalid_ai_config);两者都不传 = 保持现状。
 * ★ l4_requires_optin 必须 true(后端 400 否则)。
 */
export interface AdminAiConfigUpdateRequest {
  enabled: boolean
  provider: AiProvider
  model?: string | null
  base_url?: string | null
  api_key?: string | null
  clear_api_key?: boolean
  max_auto_egress_level: number
  l4_requires_optin: boolean
  enable_inference: boolean
  enable_auto_translation: boolean
}

/**
 * POST /api/admin/ai-config/test → AdminAiConfigTestResponse。
 * 失败时 error 为结构化字符串:ai_disabled / unsupported_provider / missing_provider_config
 * / 或 AiGatewayError 子类名(后端 type(exc).__name__)。
 */
export interface AdminAiConfigTestResponse {
  ok: boolean
  provider: string
  model: string | null
  latency_ms: number
  error: string | null
}

/** POST /api/admin/users/{id}/force-logout → AdminForceLogoutResponse。 */
export interface AdminForceLogoutResponse {
  user_id: string
  revoked_after: string
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
  retryAfterMs?: number

  constructor(
    status: number,
    message: string,
    code?: string,
    body?: ApiErrorBody,
    retryAfterMs?: number,
  ) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.body = body
    this.retryAfterMs = retryAfterMs
  }
}
