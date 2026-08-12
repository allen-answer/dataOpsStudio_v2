import { apiClient } from './client'
import type { Column, JobErrorCode, JobListItem, JobStatus, RowResponse } from './types'

export interface JobResponse {
  id: string
  kind: string
  status: JobStatus
  created_at?: string
  started_at?: string | null
  finished_at?: string | null
  result_set_id: string | null
  error: string | null
  error_code: JobErrorCode | null
  message: string | null
  timings?: JobStageTimings | null
}

export interface JobStageTimings {
  queue_ms: number | null
  connect_ms: number | null
  execute_first_row_ms: number | null
  fetch_ms: number | null
  spool_ms: number | null
  total_ms: number | null
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
  has_more?: boolean | null
  page_size?: number | null
  max_result_rows?: number | null
  preview_truncated_cells?: number
  pagination_mode?: 'ordered_offset' | 'unavailable' | null
  pagination_reason?: string | null
}

export interface JobPageResponse {
  job_id: string
  result_set_id: string
  offset: number
  cached: boolean
}

export interface ListJobsParams {
  status?: JobStatus
  project_id?: string
  limit?: number
  offset?: number
}

export function listJobs(params: ListJobsParams = {}): Promise<JobListItem[]> {
  const qs = new URLSearchParams()
  if (params.status) qs.set('status', params.status)
  if (params.project_id) qs.set('project_id', params.project_id)
  qs.set('limit', String(params.limit ?? 50))
  qs.set('offset', String(params.offset ?? 0))
  return apiClient.get<JobListItem[]>(`/jobs?${qs.toString()}`)
}

export function getJob(jobId: string): Promise<JobResponse> {
  return apiClient.get<JobResponse>(`/jobs/${jobId}`)
}

/**
 * GET /jobs/{id}/result —— pending/running/success 均可拉;用于边拉边看。
 */
export function getJobResult(
  jobId: string,
  offset: number = 0,
  limit: number = 100,
): Promise<JobResultResponse> {
  const qs = new URLSearchParams({ offset: String(offset), limit: String(limit) })
  return apiClient.get<JobResultResponse>(`/jobs/${jobId}/result?${qs.toString()}`)
}

/**
 * POST /jobs/{id}/cancel —— 协作式取消,worker 在下一次 cancel check 时退出。
 * 已经终态的 job cancel 返 { cancelled: false }。
 */
export function cancelJob(jobId: string): Promise<{ cancelled: boolean }> {
  return apiClient.post<{ cancelled: boolean }>(`/jobs/${jobId}/cancel`)
}

/**
 * POST /jobs/{id}/pages —— enqueue one ordered stateless database page.
 * The server rejects unordered SQL instead of silently serving unstable pages.
 */
export function requestJobPage(jobId: string, offset: number): Promise<JobPageResponse> {
  return apiClient.post<JobPageResponse>(`/jobs/${jobId}/pages`, { offset })
}
