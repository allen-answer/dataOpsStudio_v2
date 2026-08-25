/**
 * 2.3.0 Lineage —— 血缘 API client。
 *
 * ★ 字段形状全部锚自后端契约,不臆造:
 *  - app/api/schemas.py:
 *    LineageDirection / LineageAnalyzeRequest / LineageAnalyzeResponse /
 *    LineageSubgraphNode / LineageSubgraphEdge / LineageSubgraphResponse /
 *    LineageImpactItem / LineageImpactResponse
 *  - app/api/routes/core.py:
 *    POST /projects/{pid}/lineage/analyze?refresh=   -> LineageAnalyzeResponse (201)
 *      refresh=true 时删除同 sql_hash 缓存并强制重解析(默认 false 命中缓存直接返回 cached=true)
 *      解析失败 400 lineage_parse_failed;datasource 不在项目内 404 not_found
 *    GET /projects/{pid}/lineage/subgraph?focus=&direction=&max_depth=&include_columns=
 *      direction 默认 downstream;max_depth 默认 3(ge=1 le=5,后端再 min(,5))
 *      focus 未知不 404 —— 返回只含焦点节点的空图(edge_count=0)
 *    GET /projects/{pid}/lineage/impact?focus=&max_depth=  (纯下游遍历,按 depth 分组)
 *    POST /projects/{pid}/lineage/export body {focus, direction, max_depth, include_columns}
 *      同步生成 xlsx(三 sheet:table_edges / column_edges / impact),201 返回
 *      一次性下载 token(GET /exports/{token} 取文件;429 export_rate_limited)
 *    PATCH /projects/{pid}/lineage/edges/{edge_id} body {"inference_status": ...}
 *      AI 推断边状态机(inferred → confirmed / rejected);非 inferred 起点返回
 *      409 invalid_inference_transition;rejected 边被子图/影响 CTE 排除
 * 契约测试权威来源:tests/contract/test_api.py(搜 test_lineage_*)。
 */
import { apiClient } from './client'

export type LineageDirection = 'upstream' | 'downstream' | 'both'

export interface LineageAnalyzeRequest {
  datasource_id: string
  sql_text: string
  source_ref: string
  dialect?: string | null
  default_schema?: string | null
  /**
   * DDL 文本数据源(对标 DataGrip DDL data source):贴一段建表 DDL 补齐列元数据,
   * 让元数据缓存缺失的表也能出列级血缘。不给 = 行为与从前完全一致;元数据缓存里
   * 已有的表永远以缓存为准,DDL 只填空洞。上限 schemas.py LINEAGE_DDL_MAX_CHARS。
   */
  ddl_text?: string | null
}

/** DDL 语句被跳过的原因,锚 app/domain/lineage/ddl_schema.py SKIP_*。 */
export type LineageDdlSkipReason =
  | 'non_create_table'
  | 'ctas'
  | 'constraints_only'
  | 'parse_failed'
  | 'over_statement_limit'

/**
 * DDL 数据源解析摘要,锚 app/domain/lineage/ddl_schema.py apply_ddl_schema()。
 * 单条解析挂 parse_summary.ddl_schema;批量挂 report.ddl_schema。没给 DDL 时不存在。
 *
 * table_count / column_count 是**真正生效**的数量(合并后 DDL 实际补了多少);
 * parsed_* 是解析出来的数量,两者不等说明有表被元数据缓存遮蔽(正常,缓存优先)。
 */
export interface LineageDdlSchemaSummary {
  table_count: number
  column_count: number
  parsed_table_count?: number
  parsed_column_count?: number
  skipped_statement_count: number
  /** 只含非零原因;未知键宽容降级。 */
  skipped_reasons?: Partial<Record<LineageDdlSkipReason, number>>
  failed_column_entry_count?: number
  /** 实际采用的方言(auto 识别时用户才知道 DDL 按什么解的)。 */
  dialect?: string
  /** DDL 整段被丢弃时的原因;存在即表示"提供了但没用上"。 */
  error?: string
}

/**
 * parse_summary 后端是开放 JSON(dict[str, Any],来自 LineageReport.report 摘要 +
 * parser_version)。已知键锚 app/domain/lineage/parser.py _summary();未知键宽容降级。
 */
export interface LineageParseSummary {
  statement_count?: number
  table_edge_count?: number
  column_mapping_count?: number
  parse_error_count?: number
  parser_version?: string
  ddl_schema?: LineageDdlSchemaSummary
  [key: string]: unknown
}

export interface LineageAnalyzeResponse {
  run_id: string
  project_id: string
  datasource_id: string
  dialect: string
  source_ref: string
  sql_hash: string
  parser_version: string
  status: 'success' | 'failed'
  cached: boolean
  parse_summary: LineageParseSummary
  table_edge_count: number
  column_edge_count: number
}

export interface LineageSubgraphNode {
  id: string
  label: string
  kind: 'table' | 'column'
  table: string
  column: string | null
  depth: number
}

export interface LineageSubgraphEdge {
  id: string
  source: string
  target: string
  source_table: string
  target_table: string
  source_column: string | null
  target_column: string | null
  depth: number
  direction: 'upstream' | 'downstream'
  edge_kind: string
  inferred: boolean
  inference_status: string
  confidence: number
  transformation: string | null
  transformation_subtype: string | null
}

export interface LineageSubgraphResponse {
  project_id: string
  focus: string
  direction: LineageDirection
  max_depth: number
  include_columns: boolean
  truncated: boolean
  node_count: number
  edge_count: number
  /** Pydantic dict[int, int] —— JSON 序列化后键是字符串。 */
  depth_counts: Record<string, number>
  nodes: LineageSubgraphNode[]
  edges: LineageSubgraphEdge[]
}

export interface LineageImpactItem {
  node: string
  table: string
  column: string | null
  depth: number
  paths: string[][]
}

export interface LineageImpactResponse {
  project_id: string
  focus: string
  max_depth: number
  truncated: boolean
  impact_count: number
  impacts: LineageImpactItem[]
}

// ── endpoints ───────────────────────────────────────────────────────

/** POST /projects/{pid}/lineage/analyze —— refresh=true 绕过 sql_hash 缓存强制重解析。 */
export function analyzeLineage(
  projectId: string,
  req: LineageAnalyzeRequest,
  refresh = false,
): Promise<LineageAnalyzeResponse> {
  const qs = refresh ? '?refresh=true' : ''
  return apiClient.post<LineageAnalyzeResponse>(
    `/projects/${projectId}/lineage/analyze${qs}`,
    req,
  )
}

export interface LineageSubgraphParams {
  focus: string
  direction?: LineageDirection
  maxDepth?: number
  includeColumns?: boolean
}

export function getLineageSubgraph(
  projectId: string,
  params: LineageSubgraphParams,
): Promise<LineageSubgraphResponse> {
  const qs = new URLSearchParams({ focus: params.focus })
  if (params.direction) qs.set('direction', params.direction)
  if (params.maxDepth != null) qs.set('max_depth', String(params.maxDepth))
  if (params.includeColumns != null) qs.set('include_columns', String(params.includeColumns))
  return apiClient.get<LineageSubgraphResponse>(
    `/projects/${projectId}/lineage/subgraph?${qs.toString()}`,
  )
}

/** PATCH body 只接受两个终态(schemas.py LineageEdgeInferenceUpdateRequest)。 */
export type LineageInferenceDecision = 'confirmed' | 'rejected'

/** 锚 schemas.py LineageEdgeInferenceResponse。 */
export interface LineageEdgeInferenceResponse {
  edge_id: string
  edge_kind: 'table' | 'column'
  inference_status: string
  inferred: boolean
  confidence: number
}

/**
 * PATCH /projects/{pid}/lineage/edges/{edge_id} —— 确认 / 拒绝 AI 推断边。
 * 非 inferred 起点(已确认/已拒绝/确定性边)→ 409 invalid_inference_transition。
 */
export function updateLineageEdge(
  projectId: string,
  edgeId: string,
  status: LineageInferenceDecision,
): Promise<LineageEdgeInferenceResponse> {
  return apiClient.patch<LineageEdgeInferenceResponse>(
    `/projects/${projectId}/lineage/edges/${edgeId}`,
    { inference_status: status },
  )
}

/** 锚 schemas.py LineageEdgeRunInfo(sql_text 在 0018 前的旧 run 为 null)。 */
export interface LineageEdgeRunInfo {
  run_id: string
  source_ref: string
  dialect: string
  created_at: string
  sql_text: string | null
}

/** 锚 schemas.py LineageEdgeDetailResponse。 */
export interface LineageEdgeDetailResponse {
  edge_id: string
  edge_kind: 'table' | 'column'
  source_table: string
  source_column: string | null
  target_table: string
  target_column: string | null
  transformation: string | null
  transformation_subtype: string | null
  inferred: boolean
  inference_status: string
  confidence: number
  run: LineageEdgeRunInfo | null
}

/**
 * GET /projects/{pid}/lineage/edges/{edge_id} —— 边详情(边抽屉):
 * 边元数据 + 所属解析 run 溯源(source_ref / dialect / SQL 原文)。
 */
export function getLineageEdgeDetail(
  projectId: string,
  edgeId: string,
): Promise<LineageEdgeDetailResponse> {
  return apiClient.get<LineageEdgeDetailResponse>(
    `/projects/${projectId}/lineage/edges/${edgeId}`,
  )
}

/**
 * 锚 schemas.py LineageAiEnrichmentResponse —— L-7 整份血缘报告的 AI 解读。
 * ok=false 时 error 说明原因(no_enrichment / 后端 4xx 由 apiClient 抛异常)。
 */
export interface LineageAiEnrichmentResponse {
  run_id: string
  ok: boolean
  interpretation: string | null
  provider: string | null
  model: string | null
  egress_level: number
  created_at: string | null
  error: string | null
}

/**
 * POST /projects/{pid}/lineage/runs/{run_id}/ai-enrich —— 生成整份报告的 AI 解读并
 * 追加落库(只增不改)。AI 关闭 → 409 ai_disabled;旧 run 无存档 SQL → 409
 * lineage_run_not_enrichable;provider 故障 → 502 ai_provider_error(均由 apiClient 抛)。
 */
export function enrichLineageRun(
  projectId: string,
  runId: string,
): Promise<LineageAiEnrichmentResponse> {
  return apiClient.post<LineageAiEnrichmentResponse>(
    `/projects/${projectId}/lineage/runs/${runId}/ai-enrich`,
    {},
  )
}

/**
 * GET /projects/{pid}/lineage/runs/{run_id}/ai-enrich —— 读该 run 最近一次 AI 解读;
 * 无则 200 { ok:false, error:'no_enrichment' }(不是 404)。
 */
export function getLineageRunEnrichment(
  projectId: string,
  runId: string,
): Promise<LineageAiEnrichmentResponse> {
  return apiClient.get<LineageAiEnrichmentResponse>(
    `/projects/${projectId}/lineage/runs/${runId}/ai-enrich`,
  )
}

/** 锚 schemas.py LineageExportRequest —— 与 subgraph 端点同参(POST body 形态)。 */
export interface LineageExportRequest {
  focus: string
  direction?: LineageDirection
  max_depth?: number
  include_columns?: boolean
}

/** 锚 schemas.py ExportCreateResponse(与 sql / compare 导出同形)。 */
export interface LineageExportResponse {
  job_id: string
  download_token: string
  expires_at: string
  format: string
  filename: string
}

/**
 * POST /projects/{pid}/lineage/export —— 同步生成焦点子图 + 影响分析 xlsx,
 * 201 返回一次性 token;下载复用 GET /exports/{token}(api/metadata.ts downloadExport)。
 */
export function exportLineage(
  projectId: string,
  req: LineageExportRequest,
): Promise<LineageExportResponse> {
  return apiClient.post<LineageExportResponse>(`/projects/${projectId}/lineage/export`, req)
}

export function getLineageImpact(
  projectId: string,
  focus: string,
  maxDepth = 3,
): Promise<LineageImpactResponse> {
  const qs = new URLSearchParams({ focus, max_depth: String(maxDepth) })
  return apiClient.get<LineageImpactResponse>(
    `/projects/${projectId}/lineage/impact?${qs.toString()}`,
  )
}

// ── L-8 字段多跳追溯 + C-8 逐跳血缘对比 ────────────────────────────────
// 锚 schemas.py LineageColumnTraceItem/Hop/Response、TraceCompareRequest/
// TraceCompareHopItem/TraceCompareResponse;端点见 core.py
//   GET  /projects/{pid}/lineage/column-trace?focus=&direction=&max_depth=
//   POST /projects/{pid}/lineage/trace-compare (201;dry_run=true 只预览不落库)

/** 链上一跳字段(node 为本跳字段,from_node 为它经由的上一跳字段)。 */
export interface LineageColumnTraceItem {
  node: string
  table: string
  column: string
  from_node: string
  direction: 'upstream' | 'downstream'
  edge_id: string
  transformation: string | null
  transformation_subtype: string | null
  inferred: boolean
  inference_status: string
  confidence: number
}

export interface LineageColumnTraceHop {
  depth: number
  items: LineageColumnTraceItem[]
}

export interface LineageColumnTraceResponse {
  project_id: string
  focus: string
  direction: 'upstream' | 'downstream' | 'both'
  max_depth: number
  truncated: boolean
  hop_count: number
  hops: LineageColumnTraceHop[]
}

/** GET /projects/{pid}/lineage/column-trace —— focus 必须是 table.column(否则 400)。 */
export function getLineageColumnTrace(
  projectId: string,
  focus: string,
  direction: LineageDirection = 'upstream',
  maxDepth = 5,
): Promise<LineageColumnTraceResponse> {
  const qs = new URLSearchParams({ focus, direction, max_depth: String(maxDepth) })
  return apiClient.get<LineageColumnTraceResponse>(
    `/projects/${projectId}/lineage/column-trace?${qs.toString()}`,
  )
}

export interface TraceCompareRequest {
  focus: string
  source_id: string
  target_id: string
  key_columns: string[]
  schema_name?: string | null
  max_hops?: number
  max_depth?: number
  name?: string | null
  dry_run?: boolean
}

export interface TraceCompareHopItem {
  node_id: string
  depth: number
  table: string
  column: string
  key_columns: string[]
}

export interface TraceCompareResponse {
  project_id: string
  focus: string
  source_id: string
  target_id: string
  /** dry_run=true 时为 null(只预览未落库)。 */
  workflow_id: string | null
  workflow_name: string | null
  hop_count: number
  truncated: boolean
  hops: TraceCompareHopItem[]
}

/**
 * POST /projects/{pid}/lineage/trace-compare —— 沿焦点字段上游血缘每跳生成 compare
 * 节点组装 workflow(只创建不触发)。dry_run=true 只返回预览计划。
 * focus 非列引用 → 400 column_focus_required;无上游 → 400 no_upstream_lineage;
 * 链超上限 → 400 too_many_hops。
 */
export function createTraceCompare(
  projectId: string,
  req: TraceCompareRequest,
): Promise<TraceCompareResponse> {
  return apiClient.post<TraceCompareResponse>(
    `/projects/${projectId}/lineage/trace-compare`,
    req,
  )
}

// ── C3 Copilot:加权影响解读(L2,提前交付)──────────────────────────
// 锚 schemas.py LineageAiImpactRequest / LineageImpactSignal / LineageAiImpactResponse;
// POST /projects/{pid}/lineage/ai-impact:基础影响 + 引用频率 + 负责人 → AI 加权。
// AI 关闭 → 409 ai_disabled;gateway 故障 → ok:false + error(signals 仍返回)。

export interface LineageAiImpactRequest {
  focus: string
  max_depth?: number
  window_days?: number
}

export interface LineageImpactSignal {
  node: string
  table: string
  column: string | null
  depth: number
  ref_count: number
  source_ref_count: number
  last_referenced_days_ago: number | null
}

export interface LineageAiImpactResponse {
  project_id: string
  focus: string
  max_depth: number
  window_days: number
  ok: boolean
  error: string | null
  provider: string | null
  model: string | null
  egress_level: number
  impact_count: number
  degraded: boolean
  project_owner: string | null
  assessment: string | null
  signals: LineageImpactSignal[]
}

export function weightedLineageImpact(
  projectId: string,
  req: LineageAiImpactRequest,
): Promise<LineageAiImpactResponse> {
  return apiClient.post<LineageAiImpactResponse>(
    `/projects/${projectId}/lineage/ai-impact`,
    req,
  )
}

// ── 批量分析(L-2:ZIP 上传 → job → 报告)────────────────────────────
// 锚 schemas.py LineageBatchCreateRequest / LineageBatchCreateResponse /
// LineageBatchStatusResponse;report 形状由 app/worker.py
// _execute_lineage_batch / _lineage_batch_file / _lineage_batch_script_edges 决定。

export interface LineageBatchCreateRequest {
  upload_id: string
  // 数据源可选:不给(无库纯文本导入)时必须显式给 dialect(纯宽松表级,不落子图)
  datasource_id?: string | null
  dialect?: string | null
  default_schema?: string | null
  /** DDL 文本数据源(与单条解析同形):补齐列元数据 → 把宽松表级升回列级。 */
  ddl_text?: string | null
}

export type LineageBatchJobStatus =
  | 'pending'
  | 'running'
  | 'success'
  | 'failed'
  | 'cancelled'
  | 'timeout'

/** 单文件结果(worker _lineage_batch_file;failed 项只有 source_ref/status/error)。 */
export interface LineageBatchFileEntry {
  source_ref: string
  status: 'parsed' | 'cached' | 'failed'
  run_id?: string
  table_edge_count?: number
  column_mapping_count?: number
  parse_error_count?: number
  lenient_statement_count?: number
  tables_written?: string[]
  tables_read?: string[]
  error?: string
}

/** 跨脚本依赖:文件 A 写的表被文件 B 读 → A→B(worker _lineage_batch_script_edges)。 */
export interface LineageBatchScriptEdge {
  source_file: string
  target_file: string
  tables: string[]
}

// ── L-4 语义视图(批量 report 顶层新增 semantic_view 段;worker 侧生成)──
// 后端 JSON 全 snake_case;refresh_mode 可空(单角色/无写入目标为 null)。

/** 单目标表的写入方式计数(缺失键后端补 0)。 */
export interface LineageTargetCounts {
  insert: number
  update: number
  merge: number
  delete: number
  truncate: number
}

/** 目标表整合视图:该表被哪些源写入 / 以何种刷新模式。 */
export interface LineageTargetIntegration {
  table: string
  primary_role: string
  roles: string[]
  /** truncate_insert/delete_insert/delete_insert_partial/merge/update/append/mixed;单角色可空。 */
  refresh_mode: string | null
  counts: LineageTargetCounts
  source_tables: string[]
  source_files: string[]
  titles: string[]
}

/** 表角色汇总(每表在全批次里承担的角色集合)。 */
export interface LineageTableRole {
  table: string
  roles: string[]
  primary_role: string
}

/** 语义风险项(按 level 上色展示 message)。 */
export interface LineageSemanticRisk {
  level: 'high' | 'medium' | 'low'
  type: string
  message: string
}

/** 顶层语义视图段(挂 batchReport.semantic_view)。 */
export interface LineageSemanticView {
  targets: LineageTargetIntegration[]
  table_roles: LineageTableRole[]
  /** 中文人话观察,直接展示。 */
  observations: string[]
  risks: LineageSemanticRisk[]
}

export interface LineageBatchReport {
  file_count: number
  parsed: number
  failed: number
  skipped: { non_sql: number; too_large: number; over_file_limit: number }
  table_edge_total: number
  column_mapping_total: number
  files: LineageBatchFileEntry[]
  script_edges: LineageBatchScriptEdge[]
  /** L-4 语义视图(旧 report 无此段,可选守空)。 */
  semantic_view?: LineageSemanticView
  /** DDL 文本数据源摘要;本次未给 DDL 时后端写 null,旧 report 无此键。 */
  ddl_schema?: LineageDdlSchemaSummary | null
}

export interface LineageBatchStatusResponse {
  job_id: string
  status: LineageBatchJobStatus
  error: string | null
  /** 仅 success 时非空(API 回读 worker 落的 lineage_batch_report.json)。 */
  report: LineageBatchReport | null
}

/**
 * POST /projects/{pid}/lineage/batch —— 202 入队 worker;
 * upload purpose 不对 400 invalid_upload_purpose,upload/datasource 不在项目内 404。
 */
export function createLineageBatch(
  projectId: string,
  req: LineageBatchCreateRequest,
): Promise<{ job_id: string }> {
  return apiClient.post<{ job_id: string }>(`/projects/${projectId}/lineage/batch`, req)
}

/** GET /projects/{pid}/lineage/batch/{job_id} —— 轮询状态;success 附汇总报告。 */
export function getLineageBatch(
  projectId: string,
  jobId: string,
): Promise<LineageBatchStatusResponse> {
  return apiClient.get<LineageBatchStatusResponse>(
    `/projects/${projectId}/lineage/batch/${jobId}`,
  )
}
