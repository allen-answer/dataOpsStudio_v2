/**
 * 2.4.0 Workflow —— Job DAG 编排管理 API client。
 *
 * ★ 字段形状全部锚自后端契约,不臆造:
 *  - app/api/schemas.py:
 *    WorkflowCreateRequest / WorkflowUpdateRequest / WorkflowResponse /
 *    WorkflowListItem / WorkflowRunTriggerRequest / WorkflowRunCreateResponse /
 *    WorkflowRunNodeItem / WorkflowRunStatusResponse / WorkflowRunListItem /
 *    WorkflowRunsResponse / NotifyTargetCreateRequest / NotifyTargetUpdateRequest /
 *    NotifyTargetResponse
 *  - app/domain/workflow.py:
 *    WorkflowSpec / WorkflowNode / WorkflowEdge / CronSchedule / RetryPolicy
 *    (首版节点集 SUPPORTED_WORKFLOW_NODE_KINDS_V1、变量安全字符集校验)
 *  - app/domain/notify.py: NotifyTarget(spec.notifications 内嵌形状)
 *  - app/api/routes/core.py:
 *    GET    /projects/{pid}/workflows                          -> WorkflowListItem[]
 *    POST   /projects/{pid}/workflows                          -> WorkflowResponse (201)
 *    GET    /projects/{pid}/workflows/{id}                     -> WorkflowResponse
 *    PUT    /projects/{pid}/workflows/{id}                     -> WorkflowResponse
 *    DELETE /projects/{pid}/workflows/{id}                     -> 204
 *    POST   /projects/{pid}/workflows/{id}/runs                -> WorkflowRunCreateResponse (201)
 *    GET    /projects/{pid}/workflows/{id}/runs?limit=&offset= -> WorkflowRunsResponse
 *    GET    /projects/{pid}/workflow-runs/{run_id}             -> WorkflowRunStatusResponse
 *    POST   /projects/{pid}/workflow-runs/{run_id}:cancel      -> { cancelled }
 *    POST   /projects/{pid}/workflows/{id}/notifications       -> NotifyTargetResponse (201)
 *    PUT    /projects/{pid}/workflows/{id}/notifications/{tid} -> NotifyTargetResponse
 *    DELETE /projects/{pid}/workflows/{id}/notifications/{tid} -> 204
 * 契约测试权威来源:tests/contract/test_api.py(搜 test_workflow_*)。
 *
 * ★ R2/R5(本 client 最相关):
 *  - 通知 url / 企微 token / SMTP 密码是「只写不回读」——请求带明文一次,响应
 *    (NotifyTargetResponse)无 url / 密码字段;spec.notifications 内嵌项的
 *    url_secret_ref / password_secret_ref 是内部指针,前端**绝不渲染**。
 *  - PUT workflow 整份覆盖 dag_jsonb;后端 #152 已做 preserve-on-omit(spec 不带
 *    notifications / variables 则沿用库里既有值),但前端「高级编辑」提交完整 spec
 *    时仍应带上现有字段,语义最清晰(见 WorkflowView 保存路径)。
 */
import { apiClient } from './client'
import type { JobStatus } from './types'

// ── 首版支持的节点 kind(workflow.py SUPPORTED_WORKFLOW_NODE_KINDS_V1)────
// R7 白名单更宽(job.py ALLOWED_WORKFLOW_NODE_KINDS),但白名单内、支持集外的
// kind 会被后端拒为 unsupported_node_kind;此常量仅供展示/文档,非安全边界。
export const SUPPORTED_WORKFLOW_NODE_KINDS = [
  'sql_query',
  'sql_explain',
  'compare_run',
  'lineage_analyze',
  'export_excel',
] as const

// ── 通知渠道 / 事件(notify.py Literal)──────────────────────────────
export type NotifyChannel = 'webhook' | 'wecom' | 'email'
export type NotifyEvent = 'success' | 'failed' | 'timeout' | 'cancelled' | 'all'

// ── WorkflowSpec 结构(domain/workflow.py)────────────────────────────
export interface RetryPolicy {
  max_retries: number
  backoff_seconds: number
}

export interface CronSchedule {
  cron: string
  enabled: boolean
}

export interface WorkflowEdge {
  source: string
  target: string
}

export type WorkflowOnFailure = 'abort' | 'continue' | 'branch'

export interface WorkflowNode {
  id: string
  job_kind: string
  // 节点参数(形状按 kind 而异;v1 只读展示,不做按 kind 表单)。
  payload: Record<string, unknown>
  retry_policy: RetryPolicy | null
  timeout_seconds: number
  on_failure: WorkflowOnFailure
  when: string | null
}

/**
 * spec.notifications 内嵌项(notify.py NotifyTarget)。
 * ★ url_secret_ref / password_secret_ref 是内部 SecretStore 指针,前端绝不渲染。
 */
export interface NotifyTargetInSpec {
  id: string
  channel: NotifyChannel
  url_secret_ref?: string | null
  smtp_host?: string | null
  smtp_port?: number
  smtp_from?: string | null
  smtp_to?: string[]
  smtp_user?: string | null
  password_secret_ref?: string | null
  events: NotifyEvent[]
  enabled: boolean
  timeout_seconds: number
}

// 变量值:标量 str 或 list[str](C-7;list 供 ${var | sql_in} / ${var | csv} 展开)。
export type WorkflowVariableValue = string | string[]

export interface WorkflowSpec {
  nodes: WorkflowNode[]
  edges: WorkflowEdge[]
  schedule: CronSchedule | null
  notifications: NotifyTargetInSpec[]
  variables: Record<string, WorkflowVariableValue>
}

// ── CRUD 请求 / 响应(schemas.py)─────────────────────────────────────
export interface WorkflowListItem {
  id: string
  project_id: string
  name: string
  node_count: number
  enabled: boolean
  schedule_cron: string | null
  schedule_enabled: boolean
  created_by: string | null
  created_at: string
  updated_at: string
}

export interface WorkflowResponse {
  id: string
  project_id: string
  name: string
  spec: WorkflowSpec
  enabled: boolean
  schedule_cron: string | null
  schedule_enabled: boolean
  created_by: string | null
  created_at: string
  updated_at: string
}

// spec 是原始 dict(后端故意不类型化,以把 R7 门禁错误映射成结构化 4xx)。
export interface WorkflowCreateRequest {
  name: string
  spec: Record<string, unknown>
}

export interface WorkflowUpdateRequest {
  name: string
  spec: Record<string, unknown>
}

// ── Run 触发 / 状态 / 历史(schemas.py)────────────────────────────────
export interface WorkflowRunTriggerRequest {
  // C-7 运行时变量:合并优先级 builtin < spec.variables < 触发时。
  variables: Record<string, WorkflowVariableValue>
}

export interface WorkflowRunCreateResponse {
  run_id: string
  job_id: string
  workflow_id: string
}

// 节点执行态(core.py WorkflowNodeExecStatus)。
export type WorkflowNodeStatus =
  | 'waiting'
  | 'running'
  | 'retry_wait'
  | 'success'
  | 'failed'
  | 'skipped'
  | 'cancelled'

export interface WorkflowRunNodeItem {
  node_id: string
  job_kind: string
  status: WorkflowNodeStatus
  job_id: string | null
  attempts: number
  error: string | null
}

export interface WorkflowRunStatusResponse {
  run_id: string
  workflow_id: string | null
  project_id: string
  status: JobStatus
  error: string | null
  created_at: string | null
  started_at: string | null
  finished_at: string | null
  nodes: WorkflowRunNodeItem[]
}

export interface WorkflowRunListItem {
  run_id: string
  job_id: string
  status: JobStatus
  error: string | null
  created_at: string | null
  started_at: string | null
  finished_at: string | null
}

export interface WorkflowRunsResponse {
  workflow_id: string
  limit: number
  offset: number
  has_more: boolean
  runs: WorkflowRunListItem[]
}

// ── 通知目标 CRUD(schemas.py;★ 响应无 url / 密码,R5)─────────────────
export interface NotifyTargetCreateRequest {
  channel: NotifyChannel
  // webhook / wecom:整条明文 url(含 token)——只提交一次,不回显。
  url?: string | null
  smtp_host?: string | null
  smtp_port?: number
  smtp_from?: string | null
  smtp_to?: string[]
  smtp_user?: string | null
  smtp_password?: string | null
  events?: NotifyEvent[] | null
  enabled?: boolean
  timeout_seconds?: number
}

export interface NotifyTargetUpdateRequest {
  url?: string | null
  smtp_host?: string | null
  smtp_port?: number | null
  smtp_from?: string | null
  smtp_to?: string[] | null
  smtp_user?: string | null
  smtp_password?: string | null
  events?: NotifyEvent[] | null
  enabled?: boolean | null
  timeout_seconds?: number | null
}

/** ★ R5:无 url / 密码字段;email 连接信息(host/port/from/to/user)非敏感回显。 */
export interface NotifyTargetResponse {
  id: string
  channel: string
  events: string[]
  enabled: boolean
  timeout_seconds: number
  smtp_host: string | null
  smtp_port: number | null
  smtp_from: string | null
  smtp_to: string[] | null
  smtp_user: string | null
}

// ── endpoints ───────────────────────────────────────────────────────
export function listWorkflows(projectId: string): Promise<WorkflowListItem[]> {
  return apiClient.get<WorkflowListItem[]>(
    `/projects/${encodeURIComponent(projectId)}/workflows`,
  )
}

export function getWorkflow(projectId: string, workflowId: string): Promise<WorkflowResponse> {
  return apiClient.get<WorkflowResponse>(
    `/projects/${encodeURIComponent(projectId)}/workflows/${encodeURIComponent(workflowId)}`,
  )
}

export function createWorkflow(
  projectId: string,
  req: WorkflowCreateRequest,
): Promise<WorkflowResponse> {
  return apiClient.post<WorkflowResponse>(
    `/projects/${encodeURIComponent(projectId)}/workflows`,
    req,
  )
}

export function updateWorkflow(
  projectId: string,
  workflowId: string,
  req: WorkflowUpdateRequest,
): Promise<WorkflowResponse> {
  return apiClient.put<WorkflowResponse>(
    `/projects/${encodeURIComponent(projectId)}/workflows/${encodeURIComponent(workflowId)}`,
    req,
  )
}

export function deleteWorkflow(projectId: string, workflowId: string): Promise<void> {
  return apiClient.delete<void>(
    `/projects/${encodeURIComponent(projectId)}/workflows/${encodeURIComponent(workflowId)}`,
  )
}

/** POST /workflows/{id}/runs —— 手动触发,201 拿 run_id(可选运行时 variables)。 */
export function triggerWorkflowRun(
  projectId: string,
  workflowId: string,
  variables?: Record<string, WorkflowVariableValue>,
): Promise<WorkflowRunCreateResponse> {
  const body: WorkflowRunTriggerRequest | undefined =
    variables && Object.keys(variables).length > 0 ? { variables } : undefined
  return apiClient.post<WorkflowRunCreateResponse>(
    `/projects/${encodeURIComponent(projectId)}/workflows/${encodeURIComponent(workflowId)}/runs`,
    body,
  )
}

/** GET /workflows/{id}/runs —— 历史 run 倒序分页(limit 1–100,默认 20)。 */
export function listWorkflowRuns(
  projectId: string,
  workflowId: string,
  limit = 20,
  offset = 0,
): Promise<WorkflowRunsResponse> {
  const qs = new URLSearchParams({ limit: String(limit), offset: String(offset) })
  return apiClient.get<WorkflowRunsResponse>(
    `/projects/${encodeURIComponent(projectId)}/workflows/${encodeURIComponent(workflowId)}/runs?${qs.toString()}`,
  )
}

/** GET /workflow-runs/{run_id} —— 单 run 状态 + 节点态(轮询到终态)。 */
export function getWorkflowRun(
  projectId: string,
  runId: string,
): Promise<WorkflowRunStatusResponse> {
  return apiClient.get<WorkflowRunStatusResponse>(
    `/projects/${encodeURIComponent(projectId)}/workflow-runs/${encodeURIComponent(runId)}`,
  )
}

/** POST /workflow-runs/{run_id}:cancel —— 软取消;已终态返 409 workflow_run_terminal。 */
export function cancelWorkflowRun(
  projectId: string,
  runId: string,
): Promise<{ cancelled: boolean }> {
  return apiClient.post<{ cancelled: boolean }>(
    `/projects/${encodeURIComponent(projectId)}/workflow-runs/${encodeURIComponent(runId)}:cancel`,
  )
}

export function createNotifyTarget(
  projectId: string,
  workflowId: string,
  req: NotifyTargetCreateRequest,
): Promise<NotifyTargetResponse> {
  return apiClient.post<NotifyTargetResponse>(
    `/projects/${encodeURIComponent(projectId)}/workflows/${encodeURIComponent(workflowId)}/notifications`,
    req,
  )
}

export function updateNotifyTarget(
  projectId: string,
  workflowId: string,
  targetId: string,
  req: NotifyTargetUpdateRequest,
): Promise<NotifyTargetResponse> {
  return apiClient.put<NotifyTargetResponse>(
    `/projects/${encodeURIComponent(projectId)}/workflows/${encodeURIComponent(workflowId)}/notifications/${encodeURIComponent(targetId)}`,
    req,
  )
}

export function deleteNotifyTarget(
  projectId: string,
  workflowId: string,
  targetId: string,
): Promise<void> {
  return apiClient.delete<void>(
    `/projects/${encodeURIComponent(projectId)}/workflows/${encodeURIComponent(workflowId)}/notifications/${encodeURIComponent(targetId)}`,
  )
}
