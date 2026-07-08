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
import { apiClient, downloadTokenizedExport } from './client'
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

// ── 文件源格式(C-1;schemas.py CompareFileFormat,Parquet 归次版不含)──
export type CompareFileFormat = 'csv' | 'excel'

// ── 数据引用 / 规则 / 限制(schemas.py)──────────────────────────────
export interface CompareDataRef {
  kind: 'table' | 'sql' | 'file'
  schema_name?: string | null
  table_name?: string | null
  sql?: string | null
  // kind="file"(C-1 文件源):指向已上传件(upload_id)+ 格式与解析参数。
  // 非 file kind 忽略以下字段(schemas.py CompareDataRef 条件校验)。
  upload_id?: string | null
  file_format?: CompareFileFormat | null
  sheet?: string | null // excel:空=第一个 sheet
  header_row?: number // 1-indexed 表头行(默认 1)
  encoding?: string | null // csv:空=utf-8-sig(解码失败自动 GBK 回退)
  delimiter?: string | null // csv 分隔符
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
  source_id: string | null
  target_id: string | null
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
  // file 侧无 datasource:传 null(#126 起后端条件校验,仅 table/sql 侧必填)。
  source_id: string | null
  target_id: string | null
  source_ref: CompareDataRef
  target_ref: CompareDataRef
  columns?: Column[]
  compare_rules?: Partial<CompareRulesPayload>
  run_limits?: Partial<CompareRunLimitsPayload>
}

export interface CompareTaskUpdateRequest {
  name?: string
  source_id?: string | null
  target_id?: string | null
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

// ── AI Copilot C2:残余列映射建议(egress L2 + 可选 L4 样本)──────────
// 字段锚 app/api/schemas.py: CompareAiMapSuggestRequest / CompareAiMapSuggestResponse。
export interface CompareAiMapSuggestRequest {
  source_id: string
  target_id: string
  source_table: CompareTableRequest
  target_table: CompareTableRequest
  confirmed_mappings: Record<string, string>
  include_samples: boolean
}

export interface CompareAiMapSuggestItem {
  source_column: string
  target_column: string
  confidence: number
  rationale: string | null
}

export interface CompareAiMapSuggestResponse {
  ok: boolean
  suggestions: CompareAiMapSuggestItem[]
  provider: string | null
  model: string | null
  error: string | null
  egress_level: number
  history_available: boolean
  history_count: number
  samples_included: boolean
  sample_skip_reason: string | null
  residual_source_columns: string[]
  residual_target_columns: string[]
  truncated: boolean
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

// ── run 仪表盘(schemas.py CompareRunsDashboardResponse,C-12)─
export interface CompareRunAbortReason {
  reason: string
  count: number
}

export interface CompareRunsDashboardResponse {
  project_id: string
  days: number
  total_runs: number
  status_counts: Record<string, number>
  success_rate: number
  top_abort_reasons: CompareRunAbortReason[]
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

// ── 文件源预览(schemas.py CompareFilePreviewRequest / CompareFilePreviewResponse)─
// ref.kind 必须为 "file";不落任务、不碰 DB,纯 domain reader 读 sheet/列/前 N 行。
export interface CompareFilePreviewRequest {
  ref: CompareDataRef
  limit?: number
}

export interface CompareFilePreviewResponse {
  sheets: string[] // excel:工作簿全部 sheet 名;csv 为空
  columns: string[]
  rows: unknown[][]
  row_count: number
  truncated: boolean
  detected_encoding: string | null // csv:实际生效编码(GBK 回退回显);excel 为 null
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

// ── UX-2 C-3:差异行定位 SQL(schemas.py CompareDiffSqlResponse / CompareDiffSqlSide)──
export interface CompareDiffSqlSide {
  available: boolean
  sql: string | null
  reason: string | null
}

export interface CompareDiffSqlResponse {
  run_id: string
  bucket: CompareBucket
  key_columns: string[]
  pk_count: number
  truncated: boolean
  cap: number
  source: CompareDiffSqlSide
  target: CompareDiffSqlSide
}

// ── UX-2 C-4:主键预检(schemas.py ComparePkPrecheckRequest / ComparePkPrecheckResponse)──
export interface ComparePkPrecheckRequest {
  datasource_id: string
  ref: CompareDataRef
  key_columns: string[]
}

export interface ComparePkPrecheckResponse {
  total_rows: number
  distinct_pk_rows: number
  duplicate_pk_rows: number
  null_pk_rows: number
  has_duplicates: boolean
  has_nulls: boolean
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

export function getCompareRunsDashboard(
  projectId: string,
  days = 30,
): Promise<CompareRunsDashboardResponse> {
  return apiClient.get<CompareRunsDashboardResponse>(
    `/projects/${encodeURIComponent(projectId)}/compare/runs-dashboard?days=${days}`,
  )
}

/** POST /projects/{id}/compare/ai-map-suggest —— AI Copilot C2 残余列映射建议。 */
export function aiMapSuggestCompare(
  projectId: string,
  req: CompareAiMapSuggestRequest,
): Promise<CompareAiMapSuggestResponse> {
  return apiClient.post<CompareAiMapSuggestResponse>(
    `/projects/${projectId}/compare/ai-map-suggest`,
    req,
  )
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

/**
 * POST /projects/{pid}/compare/file-preview —— 文件源上传后预览(C-1)。
 * upload 必须属本项目且 purpose=compare_source。错误码:
 * 400 invalid_ref_kind / invalid_upload_purpose / preview_failed、404 not_found。
 */
export function previewCompareFile(
  projectId: string,
  req: CompareFilePreviewRequest,
): Promise<CompareFilePreviewResponse> {
  return apiClient.post<CompareFilePreviewResponse>(
    `/projects/${projectId}/compare/file-preview`,
    req,
  )
}

export function createCompareExport(runId: string): Promise<CompareExportCreateResponse> {
  return apiClient.post<CompareExportCreateResponse>(`/compare/runs/${runId}/export`, { format: 'excel' })
}

/** 导出下载复用共享 helper(client.ts downloadTokenizedExport,#96 review #4 去重)。 */
export function downloadCompareExport(token: string, fallbackFilename: string): Promise<void> {
  return downloadTokenizedExport(token, fallbackFilename)
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

/**
 * GET /compare/runs/{run_id}/diff-sql —— UX-2 C-3:按桶主键生成两侧定位 SQL(仅文本,不执行)。
 * 文件源那一侧 available=false + reason;超过 cap 个主键时 truncated=true(SQL 只含前 cap 个)。
 */
export function getCompareRunDiffSql(
  runId: string,
  bucket: CompareBucket,
): Promise<CompareDiffSqlResponse> {
  const qs = new URLSearchParams({ bucket })
  return apiClient.get<CompareDiffSqlResponse>(`/compare/runs/${runId}/diff-sql?${qs.toString()}`)
}

/**
 * POST /projects/{pid}/compare/pk-precheck —— UX-2 C-4:建任务前主键唯一性/空值预检(单侧)。
 * 错误码:400 invalid_sql / invalid_identifier / precheck_unsupported_file / precheck_failed、
 * 403 select_not_allowed、404 not_found、502 datasource_unreachable。
 */
export function precheckComparePk(
  projectId: string,
  req: ComparePkPrecheckRequest,
): Promise<ComparePkPrecheckResponse> {
  return apiClient.post<ComparePkPrecheckResponse>(
    `/projects/${projectId}/compare/pk-precheck`,
    req,
  )
}

/** POST /compare/runs/{run_id}/ai-attribution —— AI 关闭/失败返 ok:false + error,画像不受影响。 */
export function explainCompareRun(runId: string): Promise<CompareAiAttributionResponse> {
  return apiClient.post<CompareAiAttributionResponse>(`/compare/runs/${runId}/ai-attribution`)
}
