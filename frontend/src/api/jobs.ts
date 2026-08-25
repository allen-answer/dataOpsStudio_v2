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

export interface JobExecutionMetrics {
  queued_at: string | null
  claimed_at: string | null
  connect_started_at: string | null
  connected_at: string | null
  execute_started_at: string | null
  first_row_at: string | null
  first_batch_at: string | null
  finished_reading_at: string | null
  finished_at: string | null
  queue_ms: number | null
  connect_ms: number | null
  execute_to_first_row_ms: number | null
  fetch_ms: number | null
  spool_ms: number | null
  total_ms: number | null
  rows_read: number | null
  rows_returned: number | null
  max_rows: number | null
  limit_pushdown: boolean | null
  limit_pushdown_reason: string | null
  output_limit_applied: boolean | null
  query_shape: string | null
  effective_sql_hash: string | null
  db_type: string | null
  worker_id: string | null
}

export interface JobProgressResponse {
  job_id: string
  result_set_id: string | null
  status: JobStatus
  loaded_rows: number
  result_version: number
  columns_ready: boolean
  first_batch_ready: boolean
  terminal: boolean
  error: string | null
  error_code: JobErrorCode | null
  /**
   * 会话路径的原始分类码(`category` 或 `category:driver_code`,见
   * `app/dbclients/interactive/protocol.py`)。`error_code` 是它压成 7 值枚举
   * 后的结果 —— 压完数值驱动码就没了,而排查"表结构变了"这类问题恰恰要它。
   * job 路径没有对应物(后端直接给枚举),保持 undefined。
   */
  raw_error_code?: string | null
  retry_after_ms: number
  has_new_result: boolean
  truncated: boolean | null
  has_more: boolean | null
  pagination_mode?: 'ordered_offset' | 'unavailable' | null
  pagination_reason?: string | null
  timings: JobStageTimings | null
  execution: JobExecutionMetrics | null
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

export function getJobProgress(
  jobId: string,
  afterVersion: number,
  signal?: AbortSignal,
): Promise<JobProgressResponse> {
  const qs = new URLSearchParams({ after_version: String(afterVersion) })
  return apiClient.get<JobProgressResponse>(`/jobs/${jobId}/progress?${qs.toString()}`, {
    signal,
  })
}

/**
 * GET /jobs/{id}/result —— pending/running/success 均可拉;用于边拉边看。
 */
export function getJobResult(
  jobId: string,
  offset: number = 0,
  limit: number = 100,
  signal?: AbortSignal,
): Promise<JobResultResponse> {
  const qs = new URLSearchParams({ offset: String(offset), limit: String(limit) })
  return apiClient.get<JobResultResponse>(`/jobs/${jobId}/result?${qs.toString()}`, { signal })
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
