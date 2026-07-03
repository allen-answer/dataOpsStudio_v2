/**
 * 2.2.0 Compare —— 数据对比 API client。
 *
 * ★ 字段形状全部锚自后端契约,不臆造:
 *  - app/api/schemas.py:
 *    CompareDataRef / CompareRulesPayload / CompareRunLimitsPayload /
 *    CompareTaskCreateRequest / CompareTaskUpdateRequest / CompareTaskResponse /
 *    CompareRunCreateResponse / CompareCellDiff / CompareResultRow /
 *    CompareRunResultResponse / CompareRunProfileResponse / CompareAiAttributionResponse /
 *    CompareTableRequest / CompareInferRequest / CompareInferResponse /
 *    CompareSuggestedTaskCreateRequest / CompareTaskSuggestionResponse
 *  - app/domain/compare_infer.py:
 *    ColumnMappingCandidate / PrimaryKeyCandidate / TablePairSuggestion (+ reason enums)
 *  - app/api/routes/core.py:
 *    POST   /projects/{pid}/compare/infer            -> CompareInferResponse
 *    GET    /projects/{pid}/compare/suggest-tasks    -> { suggestions }
 *    POST   /projects/{pid}/compare/draft-task       -> CompareTaskResponse (201)
 *    GET    /compare/tasks?project_id=               -> CompareTaskResponse[]
 *    POST   /compare/tasks                           -> CompareTaskResponse (201)
 *    GET    /compare/tasks/{id}                       -> CompareTaskResponse
 *    PATCH  /compare/tasks/{id}                       -> CompareTaskResponse
 *    DELETE /compare/tasks/{id}                       -> 204
 *    POST   /compare/tasks/{id}/run                   -> { job_id, run_id } (202)
 *    GET    /compare/tasks/{id}/runs?limit=&offset=   -> CompareTaskRunsResponse
 *    POST   /projects/{pid}/compare/preview           -> ComparePreviewResponse
 *    GET    /compare/runs/{run_id}/results?bucket=&offset=&limit= -> CompareRunResultResponse
 *    GET    /compare/runs/{run_id}/profile            -> CompareRunProfileResponse
 *    POST   /compare/runs/{run_id}/ai-attribution     -> CompareAiAttributionResponse
 * 契约测试权威来源:tests/contract/test_api.py(搜 test_compare_*)。
 *
 * 注:run_id 与 job_id 都能寻址 run(后端 _compare_run_for_current_user 两者皆收);
 * 结果/画像/归因端点用 run_id,本 client 同用 run_id。
 */
import { apiClient } from './client'
import type { Column, ExportFormat } from './types'

// ── 4 桶 ────────────────────────────────────────────────────────────
export type CompareBucket = 'only_source' | 'only_target' | 'diff' | 'same'
export const COMPARE_BUCKETS: CompareBucket[] = ['only_source', 'only_target', 'diff', 'same']

// ── 推断 reason 枚举(compare_infer.py StrEnum)──────────────────────
export type MappingReason = 'exact' | 'normalized' | 'type-compatible'
export type ConflictKind = 'one_to_many' | 'many_to_one' | 'many_to_many'
export type PrimaryKeyReason = 'primary_key' | 'unique_index'
export type TableSuggestionReason = 'exact' | 'normalized'
export type SchemaPolicy = 'warn' | 'strict'

// ── 数据引用 / 规则 / 限制(schemas.py)──────────────────────────────
export interface CompareDataRef {
  kind: 'table' | 'sql'
  schema_name?: string | null
  table_name?: string | null
  sql?: string | null
}

export interface CompareRulesPayload {
  key_columns: string[]
  ignore_columns: string[]
  column_mappings: Record<string, string>
  numeric_tolerance: number | null
  trim_strings: boolean
  case_insensitive: boolean
  empty_as_null: boolean
  schema_policy: SchemaPolicy
}

export interface CompareRunLimitsPayload {
  max_rows: number | null
  export_max_rows: number | null
  fetch_chunk_size: number
  compare_batch_size: number
  stream_compare: boolean
  recursive_checksum: boolean
  bisection_factor: number
  bisection_threshold: number
  max_bisection_depth: number
  sample_quick_check: boolean
  sample_size: number
  sample_confidence: number
  result_format: string
  persist_same_bucket: boolean
  query_timeout_seconds: number
  run_disk_quota_mb: number | null
}

// ── 任务 CRUD ───────────────────────────────────────────────────────
export interface CompareTaskResponse {
  id: string
  project_id: string
  name: string
  source_id: string
  target_id: string
  source_ref: CompareDataRef
  target_ref: CompareDataRef
  columns: Column[]
  compare_rules: CompareRulesPayload
  run_limits: CompareRunLimitsPayload
  created_by: string | null
  created_at: string
  updated_at: string
}

export interface CompareTaskCreateRequest {
  project_id: string
  name: string
  source_id: string
  target_id: string
  source_ref: CompareDataRef
  target_ref: CompareDataRef
  columns?: Column[]
  compare_rules?: Partial<CompareRulesPayload>
  run_limits?: Partial<CompareRunLimitsPayload>
}

export interface CompareTaskUpdateRequest {
  name?: string
  source_id?: string
  target_id?: string
  source_ref?: CompareDataRef
  target_ref?: CompareDataRef
  columns?: Column[]
  compare_rules?: Partial<CompareRulesPayload>
  run_limits?: Partial<CompareRunLimitsPayload>
}

// ── 推断(infer)─────────────────────────────────────────────────────
export interface CompareTableRequest {
  schema_name: string
  table_name: string
}

export interface ColumnMappingCandidate {
  source_column: string
  target_column: string
  source_type: string
  target_type: string
  confidence: number
  reason: MappingReason
  conflict: boolean
  conflict_kind: ConflictKind | null
}

export interface PrimaryKeyCandidate {
  source_columns: string[]
  target_columns: string[]
  confidence: number
  reason: PrimaryKeyReason
}

export interface CompareInferRequest {
  source_id: string
  target_id: string
  source_table: CompareTableRequest
  target_table: CompareTableRequest
}

export interface CompareInferResponse {
  project_id: string
  source_id: string
  target_id: string
  source_table: CompareTableRequest
  target_table: CompareTableRequest
  mappings: ColumnMappingCandidate[]
  pk_candidates: PrimaryKeyCandidate[]
  needs_manual_pk: boolean
  compare_rules: CompareRulesPayload
  columns: Column[]
}

// ── 任务建议(suggest-tasks)─────────────────────────────────────────
export interface TablePairSuggestion {
  source_schema: string
  source_table: string
  target_schema: string
  target_table: string
  confidence: number
  reason: TableSuggestionReason
}

export interface CompareTaskSuggestionResponse {
  suggestions: TablePairSuggestion[]
}

export interface CompareSuggestedTaskCreateRequest extends CompareInferRequest {
  name?: string | null
  run_limits?: Partial<CompareRunLimitsPayload>
}

// ── 运行 / 结果 ─────────────────────────────────────────────────────
export interface CompareRunCreateResponse {
  job_id: string
  run_id: string
}

// ── 历史 run(schemas.py CompareTaskRunItem / CompareTaskRunsResponse)─
export interface CompareTaskRunItem {
  run_id: string
  job_id: string
  status: string
  created_at: string
  finished_at: string | null
  bucket_counts: Record<string, number>
  sampled: boolean
}

export interface CompareTaskRunsResponse {
  task_id: string
  limit: number
  offset: number
  has_more: boolean
  runs: CompareTaskRunItem[]
}

// ── 数据预览(schemas.py ComparePreviewRequest / ComparePreviewResponse)─
export interface ComparePreviewRequest {
  datasource_id: string
  ref: CompareDataRef
  limit?: number
}

export interface ComparePreviewResponse {
  columns: string[]
  rows: unknown[][]
  row_count: number
  truncated: boolean
}

export interface CompareCellDiff {
  column: string
  source?: unknown
  target?: unknown
}

export interface CompareResultRow {
  pk: Record<string, unknown>
  source?: Record<string, unknown> | null
  target?: Record<string, unknown> | null
  cells: CompareCellDiff[]
}

// progress / diff_profile / sample_result 后端是开放 JSON(dict[str, int] / dict[str, Any])。
// 前端按 PRD §12.5/§12.6.1 已知字段读取,未知字段宽容降级。
export interface CompareProgress {
  scanned_segments?: number
  skipped_segments?: number
  skipped_rows?: number
  row_mode_segments?: number
  max_depth?: number
  [key: string]: number | undefined
}

export interface CompareNumericDelta {
  count?: number
  constant_offset?: string | number | null
  systematic_offset?: boolean
  [key: string]: unknown
}

export interface CompareColumnProfile {
  type?: string
  observed_rows?: number
  changed_rows?: number
  diff_rate?: number
  numeric_delta?: CompareNumericDelta | null
  [key: string]: unknown
}

export interface CompareMissingKeyRanges {
  only_source?: unknown[]
  only_target?: unknown[]
  [key: string]: unknown
}

export interface CompareDiffProfile {
  version?: number
  generated?: boolean
  summary?: { diff_rows?: number; same_rows?: number; paired_rows_observed?: number }
  columns?: Record<string, CompareColumnProfile>
  missing_key_ranges?: CompareMissingKeyRanges
  [key: string]: unknown
}

export interface CompareSampleResult {
  enabled?: boolean
  mode?: string
  requested_rows?: number
  sampled_rows?: number
  observed_differences?: number
  all_sampled_equal?: boolean
  confidence?: number
  difference_rate_upper_bound?: number
  [key: string]: unknown
}

export interface CompareRunResultResponse {
  job_id: string
  run_id: string
  bucket: CompareBucket
  offset: number
  limit: number
  bucket_counts: Record<CompareBucket, number>
  progress: CompareProgress
  diff_profile: CompareDiffProfile
  sample_result: CompareSampleResult | null
  rows: CompareResultRow[]
}

export interface CompareRunProfileResponse {
  job_id: string
  run_id: string
  bucket_counts: Record<CompareBucket, number>
  progress: CompareProgress
  diff_profile: CompareDiffProfile
  sample_result: CompareSampleResult | null
}

export interface CompareAiAttributionResponse {
  run_id: string
  ok: boolean
  attribution: string | null
  provider: string | null
  model: string | null
  error: string | null
  egress_level: number
}

export interface CompareExportCreateResponse {
  job_id: string
  download_token: string
  expires_at: string
  format: ExportFormat
  filename: string
}

// ── endpoints ───────────────────────────────────────────────────────
export function listCompareTasks(projectId?: string): Promise<CompareTaskResponse[]> {
  const qs = projectId ? `?project_id=${encodeURIComponent(projectId)}` : ''
  return apiClient.get<CompareTaskResponse[]>(`/compare/tasks${qs}`)
}

export function getCompareTask(taskId: string): Promise<CompareTaskResponse> {
  return apiClient.get<CompareTaskResponse>(`/compare/tasks/${taskId}`)
}

export function createCompareTask(req: CompareTaskCreateRequest): Promise<CompareTaskResponse> {
  return apiClient.post<CompareTaskResponse>('/compare/tasks', req)
}

export function updateCompareTask(
  taskId: string,
  req: CompareTaskUpdateRequest,
): Promise<CompareTaskResponse> {
  return apiClient.patch<CompareTaskResponse>(`/compare/tasks/${taskId}`, req)
}

export function deleteCompareTask(taskId: string): Promise<void> {
  return apiClient.delete<void>(`/compare/tasks/${taskId}`)
}

export function cloneCompareTask(taskId: string): Promise<CompareTaskResponse> {
  return apiClient.post<CompareTaskResponse>(`/compare/tasks/${encodeURIComponent(taskId)}/clone`)
}

export function inferCompareTask(
  projectId: string,
  req: CompareInferRequest,
): Promise<CompareInferResponse> {
  return apiClient.post<CompareInferResponse>(`/projects/${projectId}/compare/infer`, req)
}

export function suggestCompareTasks(
  projectId: string,
  sourceId: string,
  targetId: string,
): Promise<CompareTaskSuggestionResponse> {
  const qs = new URLSearchParams({ source_id: sourceId, target_id: targetId })
  return apiClient.get<CompareTaskSuggestionResponse>(
    `/projects/${projectId}/compare/suggest-tasks?${qs.toString()}`,
  )
}

export function draftCompareTask(
  projectId: string,
  req: CompareSuggestedTaskCreateRequest,
): Promise<CompareTaskResponse> {
  return apiClient.post<CompareTaskResponse>(`/projects/${projectId}/compare/draft-task`, req)
}

/** POST /compare/tasks/{id}/run —— 202 异步入队 COMPARE_RUN job,返回 job_id + run_id。 */
export function runCompareTask(taskId: string): Promise<CompareRunCreateResponse> {
  return apiClient.post<CompareRunCreateResponse>(`/compare/tasks/${taskId}/run`)
}

/** GET /compare/tasks/{id}/runs —— 历史 run 倒序分页(limit 1–100,默认 20)。 */
export function listCompareTaskRuns(
  taskId: string,
  limit = 20,
  offset = 0,
): Promise<CompareTaskRunsResponse> {
  const qs = new URLSearchParams({ limit: String(limit), offset: String(offset) })
  return apiClient.get<CompareTaskRunsResponse>(
    `/compare/tasks/${encodeURIComponent(taskId)}/runs?${qs.toString()}`,
  )
}

/**
 * POST /projects/{pid}/compare/preview —— 不落任务先看数据(表引用或只读 SQL,limit<=200)。
 * 错误码:400 invalid_sql / invalid_identifier / preview_failed、403 select_not_allowed、
 * 502 datasource_unreachable。
 */
export function previewCompareData(
  projectId: string,
  req: ComparePreviewRequest,
): Promise<ComparePreviewResponse> {
  return apiClient.post<ComparePreviewResponse>(`/projects/${projectId}/compare/preview`, req)
}

export function createCompareExport(runId: string): Promise<CompareExportCreateResponse> {
  return apiClient.post<CompareExportCreateResponse>(`/compare/runs/${runId}/export`, { format: 'excel' })
}

export async function downloadCompareExport(
  token: string,
  fallbackFilename: string,
): Promise<void> {
  const { blob, filename } = await apiClient.downloadBlob(`/exports/${encodeURIComponent(token)}`)
  const url = URL.createObjectURL(blob)
  try {
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = filename ?? fallbackFilename
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
  } finally {
    URL.revokeObjectURL(url)
  }
}

export function getCompareRunResults(
  runId: string,
  bucket: CompareBucket,
  offset = 0,
  limit = 100,
): Promise<CompareRunResultResponse> {
  const qs = new URLSearchParams({ bucket, offset: String(offset), limit: String(limit) })
  return apiClient.get<CompareRunResultResponse>(`/compare/runs/${runId}/results?${qs.toString()}`)
}

export function getCompareRunProfile(runId: string): Promise<CompareRunProfileResponse> {
  return apiClient.get<CompareRunProfileResponse>(`/compare/runs/${runId}/profile`)
}

/** POST /compare/runs/{run_id}/ai-attribution —— AI 关闭/失败返 ok:false + error,画像不受影响。 */
export function explainCompareRun(runId: string): Promise<CompareAiAttributionResponse> {
  return apiClient.post<CompareAiAttributionResponse>(`/compare/runs/${runId}/ai-attribution`)
}
