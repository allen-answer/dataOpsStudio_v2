import { apiClient } from './client'
import type {
  DatasourceCreateRequest,
  DatasourceListItem,
  DatasourceResponse,
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
 * 同步:后端走 worker 测连接然后阻塞到 job terminal,返回 {ok: bool}。
 * 大概率 < 3s,超时(worker 没空闲 / 慢首行)后端会返 4xx + cancel job。
 */
export function testDatasource(datasourceId: string): Promise<{ ok: boolean }> {
  return apiClient.post<{ ok: boolean }>(`/datasources/${datasourceId}/test`)
}
