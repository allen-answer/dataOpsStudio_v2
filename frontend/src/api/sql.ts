import { apiClient } from './client'
import type { SqlExecuteResponse } from './types'

export interface SqlExecuteRequest {
  datasource_id: string
  sql: string
  params?: Record<string, unknown>
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
  })
}
