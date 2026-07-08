/**
 * AI Copilot —— 前端 API client。
 *
 * ★ 字段形状锚自后端契约,不臆造:
 *  - app/api/schemas.py: SqlGenerateRequest / SqlGenerateResponse
 *  - app/api/routes/core.py:
 *    POST /datasources/{datasource_id}/ai/sql-generate -> SqlGenerateResponse
 *      · AI 未启用 -> 409 { error: "ai_disabled" }(前端走禁用提示分支)
 *      · gateway 失败 -> 200 { ok:false, error }(优雅降级,不插入 SQL)
 * 契约测试权威来源:tests/contract/test_api.py(搜 test_ai_sql_generate_*)。
 * 设计:docs/design/C1-copilot-nl2sql.md(egress L2,只送 schema 结构,不含行值)。
 */
import { apiClient } from './client'

export interface SqlGenerateRequest {
  natural_language: string
  schema_name?: string | null
  table_names?: string[]
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
    },
  )
}
