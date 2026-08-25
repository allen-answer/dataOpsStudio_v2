/**
 * W2 SQL Workspace —— 元数据浏览 / SQL 工具 / 结果导出 API client。
 *
 * 字段形状全部锚自后端契约:
 *  - app/api/schemas.py: MetadataSchemaItem / MetadataTableItem / MetadataColumnItem /
 *    SqlFormatRequest / SqlFormatResponse / SqlExpandStarRequest / SqlExpandStarResponse /
 *    ExportCreateRequest / ExportCreateResponse
 *  - app/api/routes/core.py:
 *    GET  /datasources/{id}/metadata/schemas?refresh=
 *    GET  /datasources/{id}/metadata/tables?schema=&refresh=
 *    GET  /datasources/{id}/metadata/columns?schema=&table=&refresh=
 *    POST /sql/format            { sql, dialect } -> { formatted_sql }
 *    POST /sql/expand-star       { sql, datasource_id } -> { expanded_sql }
 *    POST /jobs/{job_id}/export  { format, table_name? } -> ExportCreateResponse
 *    GET  /exports/{token}       一次性二进制下载(410 已用/过期)
 * 契约测试权威来源:tests/contract/test_api.py(test_metadata_* / test_sql_format /
 *    test_sql_expand_star_* / test_create_export_* / test_export_download_token_is_one_time)。
 */
import { apiClient, downloadTokenizedExport } from './client'
import type { ExportFormat } from './types'

// GET /datasources/{id}/metadata/schemas —— MetadataSchemaItem
export interface MetadataSchemaItem {
  name: string
}

// GET /datasources/{id}/metadata/tables —— MetadataTableItem
export interface MetadataTableItem {
  schema_name: string
  name: string
  table_type: string | null
}

// GET /datasources/{id}/metadata/columns —— MetadataColumnItem
export interface MetadataColumnItem {
  name: string
  type: string
  driver_type: string | null
  nullable: boolean
  primary_key: boolean
  comment: string | null
}

// POST /sql/format
export interface SqlFormatResponse {
  formatted_sql: string
}

// POST /sql/expand-star
export interface SqlExpandStarResponse {
  expanded_sql: string
  /** 这份列清单所依据的元数据缓存写入时间(多表取最旧);旧缓存行可能没有。 */
  metadata_refreshed_at?: string | null
}

// POST /jobs/{job_id}/export
export interface ExportCreateResponse {
  job_id: string
  download_token: string
  expires_at: string
  format: ExportFormat
  filename: string
}

function metadataQuery(params: Record<string, string | boolean | undefined>): string {
  const qs = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === false) continue
    qs.set(key, value === true ? '1' : value)
  }
  const suffix = qs.toString()
  return suffix ? `?${suffix}` : ''
}

export function listMetadataSchemas(
  datasourceId: string,
  refresh = false,
): Promise<MetadataSchemaItem[]> {
  return apiClient.get<MetadataSchemaItem[]>(
    `/datasources/${datasourceId}/metadata/schemas${metadataQuery({ refresh })}`,
  )
}

export function listMetadataTables(
  datasourceId: string,
  schema: string,
  refresh = false,
): Promise<MetadataTableItem[]> {
  return apiClient.get<MetadataTableItem[]>(
    `/datasources/${datasourceId}/metadata/tables${metadataQuery({ schema, refresh })}`,
  )
}

export function listMetadataColumns(
  datasourceId: string,
  schema: string,
  table: string,
  refresh = false,
): Promise<MetadataColumnItem[]> {
  return apiClient.get<MetadataColumnItem[]>(
    `/datasources/${datasourceId}/metadata/columns${metadataQuery({ schema, table, refresh })}`,
  )
}

export function formatSql(sql: string, dialect: string): Promise<SqlFormatResponse> {
  return apiClient.post<SqlFormatResponse>('/sql/format', { sql, dialect })
}

/**
 * `refresh=true` 让后端先重读列元数据再展开(用户显式点的那一次)。默认走缓存 ——
 * 展开保持"纯缓存、不建连",带 refresh 时才可能 503 metadata_probe_failed。
 */
export function expandSqlStar(
  sql: string,
  datasourceId: string,
  refresh = false,
): Promise<SqlExpandStarResponse> {
  return apiClient.post<SqlExpandStarResponse>('/sql/expand-star', {
    sql,
    datasource_id: datasourceId,
    refresh,
  })
}

/**
 * POST /jobs/{job_id}/export —— 202;返回一次性下载 token。
 * 后端 429 export_rate_limited(每小时 10 次),409/404 源结果失效。
 */
export function createExport(
  jobId: string,
  format: ExportFormat,
  tableName?: string,
): Promise<ExportCreateResponse> {
  const body: { format: ExportFormat; table_name?: string } = { format }
  if (tableName && tableName.trim()) body.table_name = tableName.trim()
  return apiClient.post<ExportCreateResponse>(`/jobs/${jobId}/export`, body)
}

/**
 * downloadExport —— GET /exports/{token} 鉴权取 blob 后触发浏览器下载。
 * token 一次性:用过/过期后端返 410,调用方据 ApiError.status===410 提示「重新导出」。
 * 实现复用共享 helper(client.ts downloadTokenizedExport,#96 review #4 去重)。
 */
export function downloadExport(token: string, fallbackFilename: string): Promise<void> {
  return downloadTokenizedExport(token, fallbackFilename)
}
