import { apiClient } from './client'
import type {
  DatasourceCreateRequest,
  DatasourceListItem,
  DatasourceResponse,
  DatasourceTestResponse,
  DatasourceUpdateRequest,
} from './types'

/**
 * 列项目下可见 datasource。后端按 project_id filter,无项目 owner / 成员
 * 关系则返回 []。
 */
export function listDatasources(projectId?: string): Promise<DatasourceListItem[]> {
  const query = projectId ? `?project_id=${encodeURIComponent(projectId)}` : ''
  return apiClient.get<DatasourceListItem[]>(`/datasources${query}`)
}

export function getDatasource(datasourceId: string): Promise<DatasourceResponse> {
  return apiClient.get<DatasourceResponse>(`/datasources/${datasourceId}`)
}

export function createDatasource(req: DatasourceCreateRequest): Promise<DatasourceResponse> {
  return apiClient.post<DatasourceResponse>('/datasources', req)
}

/**
 * PUT /datasources/{id} —— 全字段可选。password 缺省 / 空字符串 = 不改密码。
 */
export function updateDatasource(
  datasourceId: string,
  req: DatasourceUpdateRequest,
): Promise<DatasourceResponse> {
  return apiClient.put<DatasourceResponse>(`/datasources/${datasourceId}`, req)
}

/**
 * DELETE /datasources/{id} —— 成功 204(无 body);被既有 job 引用时后端返 409
 * + DatasourceDeleteBlocked 体(由 ApiError.body 携带,调用方据 status===409 取 references)。
 */
export function deleteDatasource(datasourceId: string): Promise<void> {
  return apiClient.delete<void>(`/datasources/${datasourceId}`)
}

/**
 * 同步:后端走 worker 测连接然后阻塞到 job terminal。
 * 返回 ok + server_version + latency_ms +(失败时)结构化 error_code。
 */
export function testDatasource(datasourceId: string): Promise<DatasourceTestResponse> {
  return apiClient.post<DatasourceTestResponse>(`/datasources/${datasourceId}/test`)
}
