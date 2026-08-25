/**
 * SQL 控制台会话(Session Broker,设计 §3.1)。
 *
 * 两条路径**长期共存**:会话路径(本文件)与 job 路径(`jobs.ts` + `sql.ts`)。
 * 前端按 attach 响应路由 —— `console_session_disabled`(部署开关关)与
 * `console_session_unsupported`(方言无会话实现)两个 409 即整体回落 job 路径
 * (设计 §3.3),不是错误提示。
 *
 * 语句 progress / result 与 `JobProgressResponse` / `JobResultResponse`
 * **字段镜像**(设计 §3.2),差异只在主键与状态枚举 —— 这让工作台的自适应退避、
 * 跨 tab lease、渐进分页与虚拟化前端资产原样复用,见 `toJobProgress`。
 */
import { apiClient } from './client'
import type { JobProgressResponse, JobResultResponse } from './jobs'
import type { ExportCreateResponse } from './metadata'
import type { Column, ExportFormat, JobErrorCode, JobStatus, RowResponse } from './types'

/** `console_sessions.state`(设计 §5.1 / §2.1)。 */
export type SessionState =
  | 'connecting'
  | 'idle'
  | 'executing'
  | 'cancelling'
  | 'closing'
  | 'closed'
  | 'session_lost'
  | 'connect_failed'

/** `console_statements.state`(设计 §5.2 / §2.2)。 */
export type StatementState =
  | 'accepted'
  | 'executing'
  | 'streaming'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'timeout'
  | 'outcome_unknown'
  | 'skipped'

/**
 * 该 datasource 的硬取消能力(设计 §4.1 SP_CANCEL 权限探测)。
 * `degraded` = 取消降级为「软取消 + destroy 兜底」,前端必须明示
 * 「取消将断开会话」,不能让用户以为取消是无损的。
 */
export type ServerCancelState = 'available' | 'degraded' | 'unknown'

/** 仍可提交/取消的会话态;其余态一律要求重新 attach。 */
export const ACTIVE_SESSION_STATES: ReadonlySet<SessionState> = new Set<SessionState>([
  'connecting',
  'idle',
  'executing',
  'cancelling',
])

/** 会话已不可续用(顶栏 banner + 「重新连接」的判据,设计 §3.3)。 */
export const DEAD_SESSION_STATES: ReadonlySet<SessionState> = new Set<SessionState>([
  'closed',
  'session_lost',
  'connect_failed',
])

/**
 * 409 语义族(设计 §3.1)。前两个是**路由信号**不是故障:
 * 命中即整体回落 job 路径;其余是会话状态冲突,按各自 UX 处理。
 */
export const SESSION_FALLBACK_CODES: ReadonlySet<string> = new Set([
  'console_session_disabled',
  'console_session_unsupported',
])

export interface SessionResponse {
  session_id: string
  /** 本次 attach 分配到的 epoch;与 current_epoch 相等即「我仍是当前持有者」。 */
  epoch: number
  current_epoch: number
  state: SessionState
  db_type: string
  server_cancel: ServerCancelState
  current_statement_id: string | null
  idle_deadline: string | null
  last_activity_at: string
  close_reason: string | null
  error_code: string | null
}

export interface StatementSubmitRequest {
  epoch: number
  sql: string
  /** 幂等回执键:同 (session_id, client_request_id) 重发只回原语句行,永不重执行。 */
  client_request_id: string
  page_size?: number
  max_result_rows?: number
  /** 0 = 不限(DataGrip 语义,设计 §4.2);省略取 broker 默认 600s。 */
  timeout_seconds?: number
}

export interface StatementSubmitResponse {
  statement_id: string
  result_set_id: string | null
  seq: number
  deduplicated: boolean
}

/** progress 内嵌的会话块:执行期间一条轮询同时驱动语句与会话渲染(设计 §3.2)。 */
export interface StatementSessionBlock {
  session_id: string
  state: SessionState
  current_epoch: number
}

export interface StatementProgressResponse {
  statement_id: string
  session: StatementSessionBlock
  result_set_id: string | null
  state: StatementState
  loaded_rows: number
  result_version: number
  columns_ready: boolean
  first_batch_ready: boolean
  terminal: boolean
  error: string | null
  error_code: string | null
  retry_after_ms: number
  has_new_result: boolean
  truncated: boolean | null
  has_more: boolean | null
  pagination_mode?: 'ordered_offset' | 'unavailable' | null
  pagination_reason?: string | null
  timings: JobProgressResponse['timings']
  execution: JobProgressResponse['execution']
}

export interface StatementResultResponse {
  statement_id: string
  statement_state: StatementState
  result_set_id: string
  offset: number
  limit: number
  columns: Column[]
  rows: RowResponse[]
  loaded_rows: number | null
  total_rows: number | null
  state: string | null
  truncated: boolean | null
  has_more: boolean | null
  page_size: number | null
  max_result_rows: number | null
  preview_truncated_cells: number
  pagination_mode?: 'ordered_offset' | 'unavailable' | null
  pagination_reason?: string | null
}

export interface StatementCancelResponse {
  accepted: boolean
  statement_state: StatementState
}

/**
 * 语句态 → job 状态词汇。工作台的状态徽标 / 计时 / 结果可见性判据全部按
 * `JobStatus` 写成,映射一次就整套复用(设计 §3.2「UI 状态模型不改」)。
 *
 * `streaming` 归 running:边跑边落 spool,渐进结果照常渲染。
 * `outcome_unknown` 归 failed:只读片不产生该态,写阶段它意味着「结果未知」,
 * 宁可显示失败也不能显示成功。`skipped` 归 cancelled:批次被前序语句中断。
 */
const STATEMENT_STATE_TO_JOB_STATUS: Record<StatementState, JobStatus> = {
  accepted: 'pending',
  executing: 'running',
  streaming: 'running',
  succeeded: 'success',
  failed: 'failed',
  cancelled: 'cancelled',
  timeout: 'timeout',
  outcome_unknown: 'failed',
  skipped: 'cancelled',
}

export function statementJobStatus(state: StatementState): JobStatus {
  return STATEMENT_STATE_TO_JOB_STATUS[state]
}

/**
 * `ClassifiedError.error_code`(`category` 或 `category:driver_code`,
 * `app/dbclients/interactive/protocol.py`)→ 前端 7 值 `JobErrorCode`。
 * 数值驱动码只喂分类,**不参与任何判据** —— 错误消息文本与语言在生产是配置项。
 * 原始码本身经 `raw_error_code` 原样带进 UI 当排查线索(只显示,不分支)。
 */
const ERROR_CATEGORY_TO_JOB_CODE: Record<string, JobErrorCode> = {
  auth_failed: 'permission_denied',
  host_unreachable: 'connection_failed',
  login_timeout: 'connection_failed',
  connection_dead: 'connection_failed',
  connection_aborted: 'connection_failed',
  connect_failed: 'connection_failed',
  cancelled: 'cancelled',
  timeout: 'timeout',
  statement_error: 'sql_failed',
}

export function statementJobErrorCode(errorCode: string | null): JobErrorCode | null {
  if (!errorCode) return null
  const category = errorCode.split(':', 1)[0]
  return ERROR_CATEGORY_TO_JOB_CODE[category] ?? 'internal'
}

/**
 * 语句 progress → job progress 形状。字段镜像的兑现点:轮询循环、增量结果读取、
 * 跨 tab 广播全部只认这一个形状,会话路径因此**零分叉**复用它们。
 * `job_id` 位置放 statement_id —— 它在轮询机器里只当身份键用。
 */
export function toJobProgress(progress: StatementProgressResponse): JobProgressResponse {
  return {
    job_id: progress.statement_id,
    result_set_id: progress.result_set_id,
    status: statementJobStatus(progress.state),
    loaded_rows: progress.loaded_rows,
    result_version: progress.result_version,
    columns_ready: progress.columns_ready,
    first_batch_ready: progress.first_batch_ready,
    terminal: progress.terminal,
    error: progress.error,
    error_code: statementJobErrorCode(progress.error_code),
    raw_error_code: progress.error_code,
    retry_after_ms: progress.retry_after_ms,
    has_new_result: progress.has_new_result,
    truncated: progress.truncated,
    has_more: progress.has_more,
    pagination_mode: progress.pagination_mode,
    pagination_reason: progress.pagination_reason,
    timings: progress.timings,
    execution: progress.execution,
  }
}

/** 语句 result → job result 形状(同形,见设计 §3.1 表)。 */
export function toJobResult(result: StatementResultResponse): JobResultResponse {
  return {
    job_id: result.statement_id,
    result_set_id: result.result_set_id,
    offset: result.offset,
    limit: result.limit,
    columns: result.columns,
    rows: result.rows,
    loaded_rows: result.loaded_rows,
    total_rows: result.total_rows,
    state: result.state,
    truncated: result.truncated,
    has_more: result.has_more,
    page_size: result.page_size,
    max_result_rows: result.max_result_rows,
    preview_truncated_cells: result.preview_truncated_cells,
    pagination_mode: result.pagination_mode,
    pagination_reason: result.pagination_reason,
  }
}

/**
 * POST /sql/sessions/attach —— 绑定或接管 console 的会话;无活会话则新建。
 * **懒 attach**:只在首次执行时调用,打开工作台不占连接(设计 §3.3)。
 * 已有活会话时不重建连接,只 bump epoch —— 这正是双 tab 接管的机制。
 */
export function attachSession(consoleId: string): Promise<SessionResponse> {
  return apiClient.post<SessionResponse>('/sql/sessions/attach', { console_id: consoleId })
}

/** GET /sql/sessions/{sid} —— 单次 observe(切 console / 出错时用,不轮询)。 */
export function observeSession(sessionId: string, signal?: AbortSignal): Promise<SessionResponse> {
  return apiClient.get<SessionResponse>(`/sql/sessions/${sessionId}`, { signal })
}

/** POST /sql/sessions/{sid}/statements —— 提交语句(202)。 */
export function submitStatement(
  sessionId: string,
  body: StatementSubmitRequest,
): Promise<StatementSubmitResponse> {
  return apiClient.post<StatementSubmitResponse>(`/sql/sessions/${sessionId}/statements`, body)
}

export function getStatementProgress(
  statementId: string,
  afterVersion: number,
  signal?: AbortSignal,
): Promise<StatementProgressResponse> {
  const qs = new URLSearchParams({ after_version: String(afterVersion) })
  return apiClient.get<StatementProgressResponse>(
    `/sql/statements/${statementId}/progress?${qs.toString()}`,
    { signal },
  )
}

export function getStatementResult(
  statementId: string,
  offset = 0,
  limit = 100,
  signal?: AbortSignal,
): Promise<StatementResultResponse> {
  const qs = new URLSearchParams({ offset: String(offset), limit: String(limit) })
  return apiClient.get<StatementResultResponse>(
    `/sql/statements/${statementId}/result?${qs.toString()}`,
    { signal },
  )
}

/**
 * POST /sql/statements/{stid}/cancel —— 排队中=出队;执行中=控制通道硬取消。
 * 取消权随 epoch 移交(M8):旧 tab 的 cancel 得 409 `stale_session_epoch`。
 */
export function cancelStatement(
  statementId: string,
  epoch: number,
): Promise<StatementCancelResponse> {
  return apiClient.post<StatementCancelResponse>(`/sql/statements/${statementId}/cancel`, { epoch })
}

/** POST /sql/sessions/{sid}/close —— 先取消在跑语句 → rollback → 断连。 */
export function closeSession(sessionId: string, epoch: number): Promise<SessionResponse> {
  return apiClient.post<SessionResponse>(`/sql/sessions/${sessionId}/close`, { epoch })
}

/**
 * POST /sql/statements/{stid}/export —— 202;返回一次性下载 token。
 * 与 job 导出**同一个下载通道**(`/exports/{token}`),故前端只在「创建」这一步
 * 分叉,轮询导出 job 与下载逻辑原样复用(设计 §3.3 导出平价)。
 */
export function createStatementExport(
  statementId: string,
  format: ExportFormat,
  tableName?: string,
): Promise<ExportCreateResponse> {
  const body: { format: ExportFormat; table_name?: string } = { format }
  if (tableName && tableName.trim()) body.table_name = tableName.trim()
  return apiClient.post<ExportCreateResponse>(`/sql/statements/${statementId}/export`, body)
}
