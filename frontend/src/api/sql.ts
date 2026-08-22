import { apiClient } from './client'
import type { SqlExecuteResponse } from './types'

export interface SqlExecuteRequest {
  datasource_id: string
  sql: string
  params?: Record<string, unknown>
  console_id?: string | null
  page_size?: number
  max_result_rows?: number
  /** @deprecated Compatibility with servers/jobs predating max_result_rows. */
  max_rows?: number
}

export interface SqlConsole {
  id: string
  name: string
  datasource_id: string | null
  sql: string
  pinned: boolean
  created_at: string
  updated_at: string
}

export interface SqlConsoleCreateRequest {
  name: string
  datasource_id?: string | null
  sql?: string
  pinned?: boolean
}

export interface SqlConsoleUpdateRequest {
  name?: string
  datasource_id?: string | null
  sql?: string
  pinned?: boolean
}

/**
 * SQL 历史条目。job 路径与会话语句(Session Broker)**合流**后按 sql_hash 去重:
 * job 条目带 job_id,会话语句条目带 statement_id,`source` 明说来自哪条路径。
 * `status` 统一成 job 状态词汇供状态徽标复用;会话独有的原始状态在
 * `statement_state`(streaming / outcome_unknown / skipped)。
 */
export interface SqlHistoryItem {
  job_id: string | null
  statement_id: string | null
  source: 'job' | 'console_statement'
  datasource_id: string | null
  datasource_name: string | null
  sql: string
  sql_hash: string
  status: string
  statement_state: string | null
  created_at: string
  finished_at: string | null
  result_set_id: string | null
}

export interface SqlHistoryParams {
  datasource_id?: string
  created_after?: string
  created_before?: string
  limit?: number
}

export interface SqlTemplate {
  id: string
  name: string
  description: string | null
  sql_text: string
  variables: string[]
  category: string
  project_id: string | null
  created_at: string
  updated_at: string
}

export interface SqlTemplateCreateRequest {
  name: string
  description?: string | null
  sql_text: string
  variables?: string[]
  category?: string
  project_id?: string | null
}

export interface SqlTemplateUpdateRequest {
  name?: string
  description?: string | null
  sql_text?: string
  variables?: string[]
  category?: string
  project_id?: string | null
}

export interface SqlTemplateListParams {
  category?: string
  project_id?: string
  q?: string
}

export interface SqlTemplateRenderResponse {
  sql_text: string
}

/**
 * POST /sql/execute —— 202 异步入队,立刻返回 job_id + result_set_id。
 * 后端只接 readonly SELECT/WITH(SqlGuardError 返 400)。
 * 终态由前端轮 GET /jobs/{job_id} 直到 success/failed/cancelled/timeout 判定。
 */
export function executeSql(req: SqlExecuteRequest): Promise<SqlExecuteResponse> {
  return apiClient.post<SqlExecuteResponse>('/sql/execute', {
    datasource_id: req.datasource_id,
    sql: req.sql,
    params: req.params ?? {},
    console_id: req.console_id ?? null,
    page_size: req.page_size,
    max_result_rows: req.max_result_rows,
    max_rows: req.max_rows,
  })
}

/**
 * POST /sql/explain —— 202 异步入队 EXPLAIN job,返回 job_id + result_set_id。
 * 与 execute 同 readonly 守卫;但不接 params(后端 400 unsupported_sql_params)。
 * 计划结果作为普通 ResultSet 落库,前端轮 job 后用 GET /jobs/{id}/result 读取。
 * 源:app/api/routes/core.py explain_sql + tests/contract/test_api.py test_sql_explain_*。
 */
export function explainSql(req: SqlExecuteRequest): Promise<SqlExecuteResponse> {
  return apiClient.post<SqlExecuteResponse>('/sql/explain', {
    datasource_id: req.datasource_id,
    sql: req.sql,
    console_id: req.console_id ?? null,
  })
}

export interface SqlPreflightFinding {
  severity: 'warning' | 'info'
  code: string
  message: string
}

export interface SqlPreflightResponse {
  findings: SqlPreflightFinding[]
}

/**
 * POST /sql/preflight —— SQL 体检(C-11 advisory)。纯文本启发式,同步返回;
 * 只提示不拦截、不连库、不入队。行数估算另走 explainSql(库内 EXPLAIN)。
 * 源:app/api/routes/core.py preflight_sql + app/services/sql_preflight.py。
 */
export function preflightSql(sql: string): Promise<SqlPreflightResponse> {
  return apiClient.post<SqlPreflightResponse>('/sql/preflight', { sql })
}

export function listSqlConsoles(): Promise<SqlConsole[]> {
  return apiClient.get<SqlConsole[]>('/sql/consoles')
}

export function createSqlConsole(req: SqlConsoleCreateRequest): Promise<SqlConsole> {
  return apiClient.post<SqlConsole>('/sql/consoles', {
    name: req.name,
    datasource_id: req.datasource_id ?? null,
    sql: req.sql ?? '',
    pinned: req.pinned ?? false,
  })
}

export function updateSqlConsole(
  consoleId: string,
  req: SqlConsoleUpdateRequest,
): Promise<SqlConsole> {
  return apiClient.patch<SqlConsole>(`/sql/consoles/${consoleId}`, req)
}

export function deleteSqlConsole(consoleId: string): Promise<void> {
  return apiClient.delete<void>(`/sql/consoles/${consoleId}`)
}

export function listSqlHistory(params: SqlHistoryParams = {}): Promise<SqlHistoryItem[]> {
  const qs = new URLSearchParams()
  if (params.datasource_id) qs.set('datasource_id', params.datasource_id)
  if (params.created_after) qs.set('created_after', params.created_after)
  if (params.created_before) qs.set('created_before', params.created_before)
  qs.set('limit', String(params.limit ?? 100))
  return apiClient.get<SqlHistoryItem[]>(`/sql/history?${qs.toString()}`)
}

export function listSqlTemplates(params: SqlTemplateListParams = {}): Promise<SqlTemplate[]> {
  const qs = new URLSearchParams()
  if (params.category) qs.set('category', params.category)
  if (params.project_id) qs.set('project_id', params.project_id)
  if (params.q) qs.set('q', params.q)
  const suffix = qs.toString()
  return apiClient.get<SqlTemplate[]>(`/sql/templates${suffix ? `?${suffix}` : ''}`)
}

export function createSqlTemplate(req: SqlTemplateCreateRequest): Promise<SqlTemplate> {
  return apiClient.post<SqlTemplate>('/sql/templates', {
    name: req.name,
    description: req.description ?? null,
    sql_text: req.sql_text,
    variables: req.variables ?? [],
    category: req.category ?? 'general',
    project_id: req.project_id ?? null,
  })
}

export function updateSqlTemplate(
  templateId: string,
  req: SqlTemplateUpdateRequest,
): Promise<SqlTemplate> {
  return apiClient.patch<SqlTemplate>(`/sql/templates/${templateId}`, req)
}

export function deleteSqlTemplate(templateId: string): Promise<void> {
  return apiClient.delete<void>(`/sql/templates/${templateId}`)
}

export function renderSqlTemplate(
  templateId: string,
  values: Record<string, string>,
): Promise<SqlTemplateRenderResponse> {
  return apiClient.post<SqlTemplateRenderResponse>(`/sql/templates/${templateId}/render`, {
    values,
  })
}
