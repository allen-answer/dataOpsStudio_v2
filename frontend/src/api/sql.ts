import { apiClient } from './client'
import type { SqlExecuteResponse } from './types'

export interface SqlExecuteRequest {
  datasource_id: string
  sql: string
  params?: Record<string, unknown>
  console_id?: string | null
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

export interface SqlHistoryItem {
  job_id: string
  datasource_id: string | null
  datasource_name: string | null
  sql: string
  sql_hash: string
  status: string
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
  })
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
