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
