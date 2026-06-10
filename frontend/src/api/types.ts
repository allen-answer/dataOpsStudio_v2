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
  truncated: boolean | null
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
