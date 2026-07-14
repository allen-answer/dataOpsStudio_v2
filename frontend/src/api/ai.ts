/**
 * AI Copilot —— 前端 API client。
 *
 * ★ 字段形状锚自后端契约,不臆造:
 *  - app/api/schemas.py: SqlGenerateRequest/Response(C1)、SlowSqlDiagnoseRequest/Response(C4)
 *  - app/api/routes/core.py:
 *    POST /datasources/{datasource_id}/ai/sql-generate -> SqlGenerateResponse(C1)
 *    POST /datasources/{datasource_id}/ai/slow-sql-diagnose -> SlowSqlDiagnoseResponse(C4)
 *      · AI 未启用 -> 409 { error: "ai_disabled" }(前端走禁用提示分支)
 *      · gateway 失败 / egress 拦截 -> 200 { ok:false, error }(优雅降级)
 * 契约测试权威来源:tests/contract/test_api.py(搜 test_ai_sql_generate_* / test_slow_sql_diagnose_*)。
 * 设计:docs/design/C1-copilot-nl2sql.md(L2)、docs/design/C4-copilot-slowsql.md(L3)。
 */
import { apiClient } from './client'

export interface SqlGenerateRequest {
  natural_language: string
  schema_name?: string | null
  table_names?: string[]
  candidate_sql?: string | null
  revision_instruction?: string | null
}

export type SqlValidationState = 'passed' | 'failed' | 'partial'
export type SqlDiagnosticCode =
  | 'provider_auth_failed'
  | 'provider_rate_limited'
  | 'provider_timeout'
  | 'provider_unreachable'
  | 'provider_reasoning_only'
  | 'provider_output_truncated'
  | 'provider_invalid_response'
  | 'metadata_probe_failed'
  | 'ai_egress_blocked'
  | 'sql_parse_failed'
  | 'sql_not_readonly'
  | 'sql_unknown_table'
  | 'sql_unknown_column'

export interface SqlTableCandidate {
  schema_name: string | null
  table_name: string
  matched_by: Array<'editor_reference' | 'table_name' | 'column_name'>
}

export interface SqlTableCandidatesResponse {
  candidates: SqlTableCandidate[]
  truncated: boolean
}

export interface SqlGenerateValidation {
  readonly: SqlValidationState
  tables: SqlValidationState
  columns: SqlValidationState
  warnings: string[]
}

export interface SqlGenerateResponse {
  ok: boolean
  sql: string | null
  explanation: string | null
  provider: string | null
  model: string | null
  error: string | null
  egress_level: number
  tables_used: string[]
  truncated: boolean
  stage: 'failed' | 'validated'
  diagnostic_code: SqlDiagnosticCode | null
  attempts: number
  reasoning_mode: 'disabled' | 'enabled' | null
  validation: SqlGenerateValidation | null
  request_id: string | null
}

export function suggestSqlTables(
  datasourceId: string,
  req: { natural_language: string; schema_name?: string | null; editor_sql?: string | null },
): Promise<SqlTableCandidatesResponse> {
  return apiClient.post<SqlTableCandidatesResponse>(
    `/datasources/${encodeURIComponent(datasourceId)}/ai/sql-table-candidates`,
    {
      natural_language: req.natural_language,
      schema_name: req.schema_name ?? null,
      editor_sql: req.editor_sql ?? null,
    },
  )
}

/** POST /datasources/{id}/ai/sql-generate —— 自然语言 → 真实 schema 生成只读 SQL。 */
export function generateSql(
  datasourceId: string,
  req: SqlGenerateRequest,
): Promise<SqlGenerateResponse> {
  return apiClient.post<SqlGenerateResponse>(
    `/datasources/${encodeURIComponent(datasourceId)}/ai/sql-generate`,
    {
      natural_language: req.natural_language,
      schema_name: req.schema_name ?? null,
      table_names: req.table_names ?? [],
      candidate_sql: req.candidate_sql ?? null,
      revision_instruction: req.revision_instruction ?? null,
    },
  )
}

export interface SlowSqlDiagnoseRequest {
  sql: string
  explain_job_id?: string | null
}

export interface SlowSqlDiagnoseResponse {
  ok: boolean
  diagnosis: string | null
  provider: string | null
  model: string | null
  error: string | null
  egress_level: number
  plan_included: boolean
  tables_analyzed: string[]
  baseline_available: boolean
  baseline_runs: number
  truncated: boolean
}

/** POST /datasources/{id}/ai/slow-sql-diagnose —— 慢 SQL 根因诊断(EXPLAIN + 结构 + 历史基线)。 */
export function diagnoseSlowSql(
  datasourceId: string,
  req: SlowSqlDiagnoseRequest,
): Promise<SlowSqlDiagnoseResponse> {
  return apiClient.post<SlowSqlDiagnoseResponse>(
    `/datasources/${encodeURIComponent(datasourceId)}/ai/slow-sql-diagnose`,
    {
      sql: req.sql,
      explain_job_id: req.explain_job_id ?? null,
    },
  )
}
