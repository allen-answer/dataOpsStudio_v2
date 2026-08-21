<script setup lang="ts">
/**
 * WorkflowView —— /projects/:id/workflows(2.4.0 Workflow 管理前端 v1)
 *
 * 单视图 master-detail(左列表 / 右详情),镜像 Compare/Lineage 范式。v1 范围:
 *  - 列表 + 创建(JSON spec)+ 删除
 *  - 详情:节点 / 边 / 调度 / 变量**只读渲染**;spec 编辑走「高级编辑」危险区
 *    (原始 JSON textarea + 确认弹窗警告整份覆盖 dag_jsonb)——不做拖拽 DAG builder,
 *    避开整份 PUT 覆盖风险(后端 #152 已 preserve-on-omit notifications/variables)。
 *  - 手动触发 run(C-7 运行时变量覆盖 UI,值内联安全字符集预校验)
 *  - 单 run 状态轮询到终态 + 取消 + run 历史列表(PR0 端点)
 *  - 通知目标 CRUD(C-9;★ 明文 url / 密码只提交一次不回显,R2/R5)
 *
 * 字段全部锚 api/workflow.ts(锚后端 schemas.py + workflow.py + notify.py),不臆造。
 */
import { computed, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import {
  Bell,
  GitBranch,
  History,
  Info,
  ListTree,
  Play,
  Plus,
  Trash2,
  X,
} from 'lucide-vue-next'
import {
  cancelWorkflowRun,
  createNotifyTarget,
  createWorkflow,
  deleteNotifyTarget,
  deleteWorkflow,
  getWorkflow,
  getWorkflowRun,
  listWorkflowRuns,
  listWorkflows,
  triggerWorkflowRun,
  updateNotifyTarget,
  updateWorkflow,
  type NotifyChannel,
  type NotifyEvent,
  type NotifyTargetInSpec,
  type WorkflowNodeStatus,
  type WorkflowVariableValue,
} from '../api/workflow'
import { listDatasources } from '../api/datasources'
import { listCompareTasks } from '../api/compare'
import { ApiError, type JobStatus } from '../api/types'
import Modal from '../components/Modal.vue'
import JobStatusBadge from '../components/JobStatusBadge.vue'
import LoadingDots from '../components/LoadingDots.vue'
import WorkflowEditor from '../components/workflow/WorkflowEditor.vue'
import {
  emptyWorkflowSpec,
  safeOutputEntries,
  specForAdvancedJson,
  type WorkflowEditorValue,
} from '../components/workflow/workflowEditor'
import { useToast } from '../composables/useToast'
import { useLicense } from '../composables/useLicense'
import { createUserErrorMessage } from '../utils/userErrorMessage'

type DetailTab = 'detail' | 'runs' | 'notify'

const { t } = useI18n()
const errorMessage = createUserErrorMessage(t)
const { writesBlocked } = useLicense()
const route = useRoute()
const toast = useToast()
const queryClient = useQueryClient()
const projectId = computed(() => (typeof route.params.id === 'string' ? route.params.id : ''))

// 运行时变量安全字符集(与后端 workflow.py 同规则;仅即时反馈,真校验在后端)
const VAR_NAME_RE = /^[A-Za-z_][A-Za-z0-9_]*$/
const VAR_VALUE_RE = /^[A-Za-z0-9_.:-]*$/
const BUILTIN_VAR_NAMES = new Set(['today', 'now', 'year', 'month', 'day'])

const TERMINAL_RUN_STATUSES = new Set<JobStatus>(['success', 'failed', 'cancelled', 'timeout'])

// ── 列表 ─────────────────────────────────────────────────────────────
const listQuery = useQuery({
  queryKey: computed(() => ['workflows', projectId.value]),
  queryFn: () => listWorkflows(projectId.value),
  enabled: computed(() => Boolean(projectId.value)),
})
const workflows = computed(() => listQuery.data.value ?? [])

const selectedId = ref<string | null>(null)
const tab = ref<DetailTab>('detail')

// 列表加载后自动选中第一个(若尚未选)
watch(workflows, (list) => {
  if (!selectedId.value && list.length > 0) selectedId.value = list[0].id
  if (selectedId.value && !list.some((w) => w.id === selectedId.value)) {
    selectedId.value = list.length > 0 ? list[0].id : null
    resetRunSelection()
  }
})

function selectWorkflow(id: string): void {
  closeEditor()
  selectedId.value = id
  tab.value = 'detail'
  selectedRunId.value = null
  runOffset.value = 0
}

// ── 详情 ─────────────────────────────────────────────────────────────
const detailQuery = useQuery({
  queryKey: computed(() => ['workflow', projectId.value, selectedId.value]),
  queryFn: () => getWorkflow(projectId.value, selectedId.value as string),
  enabled: computed(() => Boolean(projectId.value && selectedId.value)),
})
const detail = computed(() => detailQuery.data.value ?? null)

const specVariables = computed<[string, WorkflowVariableValue][]>(() =>
  Object.entries(detail.value?.spec.variables ?? {}),
)

function invalidateDetail(): void {
  void queryClient.invalidateQueries({ queryKey: ['workflow', projectId.value, selectedId.value] })
  void queryClient.invalidateQueries({ queryKey: ['workflows', projectId.value] })
}

// ── 结构化创建 / 编辑 ───────────────────────────────────────────────
type EditorIntent = 'create' | 'edit'
const editorIntent = ref<EditorIntent | null>(null)
const editorBusy = ref(false)
const editorKey = ref(0)

const datasourceQuery = useQuery({
  queryKey: computed(() => ['datasources', projectId.value]),
  queryFn: () => listDatasources(projectId.value),
  enabled: computed(() => Boolean(projectId.value && editorIntent.value)),
})
const compareTasksQuery = useQuery({
  queryKey: computed(() => ['compare-tasks', projectId.value]),
  queryFn: () => listCompareTasks(projectId.value),
  enabled: computed(() => Boolean(projectId.value && editorIntent.value)),
})

function openCreate(): void {
  if (writesBlocked.value) return
  editorIntent.value = 'create'
  editorKey.value += 1
}

function openEdit(): void {
  if (writesBlocked.value) return
  if (!detail.value) return
  editorIntent.value = 'edit'
  editorKey.value += 1
}

function closeEditor(): void {
  editorIntent.value = null
}

async function saveEditor(value: WorkflowEditorValue): Promise<void> {
  if (writesBlocked.value) {
    toast.error(t('license.writes_blocked'))
    return
  }
  editorBusy.value = true
  try {
    // Notification targets are a dedicated subresource. Never send the editor's
    // potentially stale, write-only SecretRefs back through the Workflow PUT.
    const requestSpec = specForAdvancedJson(value.spec)
    if (editorIntent.value === 'create') {
      const created = await createWorkflow(projectId.value, {
        name: value.name,
        enabled: value.enabled,
        spec: requestSpec,
      })
      toast.success(t('workflow.toast_created'))
      await queryClient.invalidateQueries({ queryKey: ['workflows', projectId.value] })
      selectedId.value = created.id
      resetRunSelection()
    } else if (selectedId.value) {
      await updateWorkflow(projectId.value, selectedId.value, {
        name: value.name,
        enabled: value.enabled,
        spec: requestSpec,
      })
      toast.success(t('workflow.toast_saved'))
      invalidateDetail()
    }
    editorIntent.value = null
    tab.value = 'detail'
  } catch (e) {
    toast.error(workflowErrorMessage(e))
  } finally {
    editorBusy.value = false
  }
}

// ── 删除 ─────────────────────────────────────────────────────────────
const deleteOpen = ref(false)
const deleteBusy = ref(false)

async function confirmDelete(): Promise<void> {
  if (writesBlocked.value) {
    toast.error(t('license.writes_blocked'))
    return
  }
  if (!selectedId.value) return
  deleteBusy.value = true
  try {
    await deleteWorkflow(projectId.value, selectedId.value)
    deleteOpen.value = false
    toast.success(t('workflow.toast_deleted'))
    selectedId.value = null
    resetRunSelection()
    await queryClient.invalidateQueries({ queryKey: ['workflows', projectId.value] })
  } catch (e) {
    toast.error(workflowErrorMessage(e))
  } finally {
    deleteBusy.value = false
  }
}

// ── 触发 run(C-7 运行时变量覆盖)────────────────────────────────────
interface VarRow {
  name: string
  value: string
}
const triggerOpen = ref(false)
const triggerVars = ref<VarRow[]>([])
const triggerError = ref<string | null>(null)
const triggerBusy = ref(false)

function openTrigger(): void {
  if (writesBlocked.value) return
  triggerVars.value = []
  triggerError.value = null
  triggerOpen.value = true
}
function addVarRow(): void {
  triggerVars.value.push({ name: '', value: '' })
}
function removeVarRow(index: number): void {
  triggerVars.value.splice(index, 1)
}

/** 本地预校验(即时反馈):名字合法/非内置、值只含安全字符集且无 `--`。 */
function validateVars(): { ok: boolean; vars: Record<string, string> } {
  const vars: Record<string, string> = {}
  for (const row of triggerVars.value) {
    const name = row.name.trim()
    if (!name) continue
    if (!VAR_NAME_RE.test(name)) {
      triggerError.value = t('workflow.err.invalid_variable_name', { name })
      return { ok: false, vars }
    }
    if (BUILTIN_VAR_NAMES.has(name)) {
      triggerError.value = t('workflow.err.variable_name_collides_builtin', { name })
      return { ok: false, vars }
    }
    if (!VAR_VALUE_RE.test(row.value) || row.value.includes('--')) {
      // R5:提示只含变量名,不回显取值
      triggerError.value = t('workflow.err.unsafe_variable_value', { name })
      return { ok: false, vars }
    }
    vars[name] = row.value
  }
  return { ok: true, vars }
}

async function submitTrigger(): Promise<void> {
  triggerError.value = null
  if (writesBlocked.value) {
    triggerError.value = t('license.writes_blocked')
    return
  }
  const { ok, vars } = validateVars()
  if (!ok) return
  triggerBusy.value = true
  try {
    const res = await triggerWorkflowRun(
      projectId.value,
      selectedId.value as string,
      Object.keys(vars).length > 0 ? vars : undefined,
    )
    triggerOpen.value = false
    toast.success(t('workflow.toast_triggered'))
    runOffset.value = 0
    selectedRunId.value = res.run_id
    tab.value = 'runs'
    void queryClient.invalidateQueries({
      queryKey: ['workflow-runs', projectId.value, selectedId.value],
    })
  } catch (e) {
    triggerError.value = workflowErrorMessage(e)
  } finally {
    triggerBusy.value = false
  }
}

// ── run 历史 + 单 run 状态 ───────────────────────────────────────────
const RUN_PAGE_SIZE = 20
const runOffset = ref(0)
const runsQuery = useQuery({
  queryKey: computed(() => [
    'workflow-runs',
    projectId.value,
    selectedId.value,
    runOffset.value,
  ]),
  queryFn: () =>
    listWorkflowRuns(
      projectId.value,
      selectedId.value as string,
      RUN_PAGE_SIZE,
      runOffset.value,
    ),
  enabled: computed(() => Boolean(projectId.value && selectedId.value && tab.value === 'runs')),
})
const runs = computed(() => runsQuery.data.value?.runs ?? [])

const selectedRunId = ref<string | null>(null)

function resetRunSelection(): void {
  runOffset.value = 0
  selectedRunId.value = null
}

const runStatusQuery = useQuery({
  queryKey: computed(() => ['workflow-run', projectId.value, selectedRunId.value]),
  queryFn: () => getWorkflowRun(projectId.value, selectedRunId.value as string),
  enabled: computed(() => Boolean(projectId.value && selectedRunId.value)),
  // 轮询到终态:非终态每 1.5s 重取,终态返回 false 停轮(设计稿 §2.3 范式)
  refetchInterval: (query) => {
    const data = query.state.data
    if (data && TERMINAL_RUN_STATUSES.has(data.status)) return false
    return 1500
  },
})
const runStatus = computed(() => runStatusQuery.data.value ?? null)
const runIsTerminal = computed(
  () => Boolean(runStatus.value && TERMINAL_RUN_STATUSES.has(runStatus.value.status)),
)

// run 终态时刷新历史列表(状态/耗时对齐)
watch(runIsTerminal, (terminal) => {
  if (terminal) {
    void queryClient.invalidateQueries({
      queryKey: ['workflow-runs', projectId.value, selectedId.value],
    })
  }
})

function openRun(runId: string): void {
  selectedRunId.value = runId
}

function previousRunPage(): void {
  runOffset.value = Math.max(0, runOffset.value - RUN_PAGE_SIZE)
  selectedRunId.value = null
}

function nextRunPage(): void {
  if (!runsQuery.data.value?.has_more) return
  runOffset.value += RUN_PAGE_SIZE
  selectedRunId.value = null
}

const cancelBusy = ref(false)
async function cancelRun(): Promise<void> {
  if (writesBlocked.value) {
    toast.error(t('license.writes_blocked'))
    return
  }
  if (!selectedRunId.value) return
  cancelBusy.value = true
  try {
    await cancelWorkflowRun(projectId.value, selectedRunId.value)
    toast.success(t('workflow.toast_cancel_requested'))
    void runStatusQuery.refetch()
  } catch (e) {
    if (e instanceof ApiError && e.code === 'workflow_run_terminal') {
      toast.error(t('workflow.err.workflow_run_terminal'))
      void runStatusQuery.refetch()
    } else {
      toast.error(workflowErrorMessage(e))
    }
  } finally {
    cancelBusy.value = false
  }
}

// ── 通知目标 CRUD(C-9)──────────────────────────────────────────────
const notifyTargets = computed<NotifyTargetInSpec[]>(() => detail.value?.spec.notifications ?? [])

interface NotifyForm {
  targetId: string | null // null = 新建
  channel: NotifyChannel
  url: string
  smtp_host: string
  smtp_port: number
  smtp_from: string
  smtp_to: string
  smtp_user: string
  smtp_password: string
  enabled: boolean
  timeout_seconds: number
  events: NotifyEvent[]
}
const NOTIFY_EVENTS: NotifyEvent[] = ['success', 'failed', 'timeout', 'cancelled', 'all']
const notifyOpen = ref(false)
const notifyBusy = ref(false)
const notifyError = ref<string | null>(null)
const notifyForm = reactive<NotifyForm>({
  targetId: null,
  channel: 'webhook',
  url: '',
  smtp_host: '',
  smtp_port: 587,
  smtp_from: '',
  smtp_to: '',
  smtp_user: '',
  smtp_password: '',
  enabled: true,
  timeout_seconds: 5,
  events: ['failed'],
})

function clearNotifyPlaintext(): void {
  notifyForm.url = ''
  notifyForm.smtp_password = ''
}

function closeNotify(): void {
  clearNotifyPlaintext()
  notifyOpen.value = false
}

function openNotifyCreate(): void {
  if (writesBlocked.value) return
  notifyForm.targetId = null
  notifyForm.channel = 'webhook'
  clearNotifyPlaintext()
  notifyForm.smtp_host = ''
  notifyForm.smtp_port = 587
  notifyForm.smtp_from = ''
  notifyForm.smtp_to = ''
  notifyForm.smtp_user = ''
  notifyForm.enabled = true
  notifyForm.timeout_seconds = 5
  notifyForm.events = ['failed']
  notifyError.value = null
  notifyOpen.value = true
}

function openNotifyEdit(target: NotifyTargetInSpec): void {
  if (writesBlocked.value) return
  notifyForm.targetId = target.id
  notifyForm.channel = target.channel
  clearNotifyPlaintext()
  notifyForm.smtp_host = target.smtp_host ?? ''
  notifyForm.smtp_port = target.smtp_port ?? 587
  notifyForm.smtp_from = target.smtp_from ?? ''
  notifyForm.smtp_to = (target.smtp_to ?? []).join(', ')
  notifyForm.smtp_user = target.smtp_user ?? ''
  notifyForm.enabled = target.enabled
  notifyForm.timeout_seconds = target.timeout_seconds
  notifyForm.events = (target.events ?? ['failed']).filter(
    (event): event is NotifyEvent => NOTIFY_EVENTS.includes(event as NotifyEvent),
  )
  notifyError.value = null
  notifyOpen.value = true
}

function smtpRecipients(): string[] {
  return notifyForm.smtp_to
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean)
}

function toggleNotifyEvent(event: NotifyEvent, checked: boolean): void {
  let next = notifyForm.events.filter((candidate) => candidate !== event)
  if (checked) next.push(event)
  if (event === 'all' && checked) next = ['all']
  if (event !== 'all' && checked) next = next.filter((candidate) => candidate !== 'all')
  notifyForm.events = next
}

async function submitNotify(): Promise<void> {
  notifyError.value = null
  if (writesBlocked.value) {
    notifyError.value = t('license.writes_blocked')
    return
  }
  if (notifyForm.events.length === 0) {
    notifyError.value = t('workflow.err.invalid_notify_events')
    return
  }
  const email = notifyForm.channel === 'email'
  const recipients = smtpRecipients()
  if (
    email &&
    (!notifyForm.smtp_host.trim() || !notifyForm.smtp_from.trim() || recipients.length === 0)
  ) {
    notifyError.value = t('workflow.err.invalid_email_target')
    return
  }
  notifyBusy.value = true
  try {
    if (notifyForm.targetId === null) {
      if (!email && !notifyForm.url.trim()) {
        notifyError.value = t('workflow.err.url_required')
        return
      }
      await createNotifyTarget(projectId.value, selectedId.value as string, {
        channel: notifyForm.channel,
        url: email ? null : notifyForm.url.trim(),
        smtp_host: email ? notifyForm.smtp_host.trim() : null,
        smtp_port: email ? notifyForm.smtp_port : undefined,
        smtp_from: email ? notifyForm.smtp_from.trim() : null,
        smtp_to: email ? recipients : undefined,
        smtp_user: email ? notifyForm.smtp_user.trim() || null : null,
        smtp_password: email ? notifyForm.smtp_password || null : null,
        enabled: notifyForm.enabled,
        timeout_seconds: notifyForm.timeout_seconds,
        events: notifyForm.events,
      })
    } else {
      await updateNotifyTarget(projectId.value, selectedId.value as string, notifyForm.targetId, {
        url: email ? null : notifyForm.url.trim() || null,
        smtp_host: email ? notifyForm.smtp_host.trim() : null,
        smtp_port: email ? notifyForm.smtp_port : null,
        smtp_from: email ? notifyForm.smtp_from.trim() : null,
        smtp_to: email ? recipients : null,
        smtp_user: email ? notifyForm.smtp_user.trim() || null : null,
        smtp_password: email ? notifyForm.smtp_password || null : null,
        enabled: notifyForm.enabled,
        timeout_seconds: notifyForm.timeout_seconds,
        events: notifyForm.events,
      })
    }
    notifyOpen.value = false
    toast.success(t('workflow.toast_notify_saved'))
    invalidateDetail()
  } catch (e) {
    notifyError.value = workflowErrorMessage(e)
  } finally {
    clearNotifyPlaintext()
    notifyBusy.value = false
  }
}

const notifyDeleteId = ref<string | null>(null)
async function confirmDeleteNotify(): Promise<void> {
  if (writesBlocked.value) {
    toast.error(t('license.writes_blocked'))
    return
  }
  if (!notifyDeleteId.value) return
  const targetId = notifyDeleteId.value
  try {
    await deleteNotifyTarget(projectId.value, selectedId.value as string, targetId)
    notifyDeleteId.value = null
    toast.success(t('workflow.toast_notify_deleted'))
    invalidateDetail()
  } catch (e) {
    notifyDeleteId.value = null
    toast.error(workflowErrorMessage(e))
  }
}

// ── helpers ─────────────────────────────────────────────────────────
// 后端结构化 error code → 人话(§1.1 R7/校验/通知错误码列表)。未知回落 message。
const KNOWN_CODES = new Set([
  'forbidden_node_kind',
  'unsupported_node_kind',
  'unsupported_on_failure',
  'duplicate_node_id',
  'unknown_edge_node',
  'self_loop',
  'cycle_detected',
  'invalid_cron',
  'invalid_node_id',
  'invalid_when',
  'invalid_branch_payload',
  'invalid_sleep_payload',
  'invalid_notify_payload',
  'invalid_edge_when',
  'duplicate_edge',
  'invalid_branch_routes',
  'conditional_edge_requires_branch',
  'missing_failure_route',
  'invalid_failure_routes',
  'failure_edge_requires_branch',
  'unknown_notify_target',
  'invalid_node_output_reference',
  'unknown_node_output',
  'forbidden_node_output_field',
  'non_upstream_node_output',
  'invalid_sensor_datasource',
  'invalid_workflow_spec',
  'workflow_name_conflict',
  'workflow_disabled',
  'workflow_run_terminal',
  'too_many_variables',
  'invalid_variable_name',
  'variable_name_collides_builtin',
  'unsafe_variable_value',
  'variable_value_too_long',
  'invalid_variable_value',
  'variable_list_too_long',
  'duplicate_notify_target_id',
  'workflow_notify_target_in_use',
  'workflow_notify_target_active_run',
  'invalid_url_secret_ref',
  'invalid_email_target',
  'invalid_notify_events',
  'invalid_notify_target_id',
])

function workflowErrorMessage(e: unknown): string {
  if (e instanceof ApiError) {
    const code = e.code ?? ''
    if (code && KNOWN_CODES.has(code)) {
      // 变量类错误码后端 message 只含变量名(R5);key 存在则用 i18n,否则回落 message
      const key = `workflow.err.${code}`
      const translated = t(key)
      if (translated !== key) return translated
    }
    return errorMessage(e)
  }
  return errorMessage(e)
}

// 节点执行态徽标色(node exec status ≠ JobStatus;自成一套语义色)
const NODE_STATUS_CLASS: Record<WorkflowNodeStatus, string> = {
  waiting: 'bg-slate-100 text-slate-600 dark:bg-slate-500/15 dark:text-slate-300',
  running: 'bg-sky-50 text-sky-700 dark:bg-sky-500/15 dark:text-sky-300',
  retry_wait: 'bg-amber-50 text-amber-700 dark:bg-amber-500/15 dark:text-amber-300',
  success: 'bg-emerald-50 text-emerald-700 dark:bg-emerald-500/15 dark:text-emerald-300',
  failed: 'bg-red-50 text-red-700 dark:bg-red-500/15 dark:text-red-300',
  skipped: 'bg-slate-100 text-slate-500 dark:bg-slate-500/10 dark:text-slate-400',
  cancelled: 'bg-slate-100 text-slate-600 dark:bg-slate-500/15 dark:text-slate-300',
}
function nodeStatusClass(status: WorkflowNodeStatus): string {
  return NODE_STATUS_CLASS[status] ?? NODE_STATUS_CLASS.waiting
}
function nodeStatusLabel(status: WorkflowNodeStatus): string {
  const key = `workflow.node_status.${status}`
  const label = t(key)
  return label === key ? status : label
}

function fmtTime(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString()
}

function outputValue(value: string | number | boolean | null): string {
  if (value === null) return '—'
  return String(value)
}

function payloadPreview(payload: Record<string, unknown>): string {
  const json = JSON.stringify(payload ?? {})
  return json.length > 80 ? `${json.slice(0, 79)}…` : json
}

</script>

<template>
  <div class="flex h-full min-h-0 chrome-bg-main">
    <!-- ============ 左栏:workflow 列表 ============ -->
    <div class="w-72 shrink-0 border-r chrome-border chrome-bg-panel flex flex-col min-h-0">
      <div class="flex items-center justify-between px-3 py-2 border-b chrome-border-subtle">
        <div class="flex items-center gap-2 text-section font-semibold chrome-text-heading">
          <GitBranch class="w-4 h-4" /> {{ t('workflow.title') }}
        </div>
        <button
          type="button"
          class="chrome-btn-primary text-xs"
          :disabled="writesBlocked"
          :title="writesBlocked ? t('license.writes_blocked') : ''"
          @click="openCreate"
        >
          <Plus class="w-3.5 h-3.5" /> {{ t('workflow.new') }}
        </button>
      </div>
      <div class="flex-1 min-h-0 overflow-auto p-2 space-y-1">
        <div v-if="listQuery.isPending.value" class="p-3"><LoadingDots /></div>
        <div
          v-else-if="workflows.length === 0"
          class="p-4 text-center text-xs chrome-text-muted"
        >
          <div class="font-medium chrome-text-heading">{{ t('workflow.empty_title') }}</div>
          <p class="mt-1">{{ t('workflow.empty_hint') }}</p>
        </div>
        <button
          v-for="wf in workflows"
          :key="wf.id"
          type="button"
          class="w-full text-left rounded-card border px-3 py-2 transition-colors"
          :class="
            wf.id === selectedId
              ? 'chrome-accent-light-bg chrome-accent border-transparent'
              : 'chrome-border chrome-text-normal hover:chrome-bg-elevated'
          "
          @click="selectWorkflow(wf.id)"
        >
          <div class="text-sm font-medium truncate">{{ wf.name }}</div>
          <div class="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px] chrome-text-muted">
            <span>{{ t('workflow.node_count', { count: wf.node_count }) }}</span>
            <span v-if="wf.schedule_cron" class="font-mono">
              {{ wf.schedule_cron }}
              <span v-if="!wf.schedule_enabled">({{ t('workflow.schedule_off') }})</span>
            </span>
            <span v-if="!wf.enabled" class="text-amber-600 dark:text-amber-400">
              {{ t('workflow.disabled') }}
            </span>
          </div>
        </button>
      </div>
    </div>

    <!-- ============ 右栏:详情 ============ -->
    <div class="flex-1 min-h-0 flex flex-col">
      <div v-if="editorIntent === 'create'" class="flex-1 min-h-0 overflow-auto p-4">
        <WorkflowEditor
          :key="editorKey"
          initial-name=""
          :initial-enabled="true"
          :initial-spec="emptyWorkflowSpec()"
          :create-mode="true"
          :datasources="datasourceQuery.data.value ?? []"
          :compare-tasks="compareTasksQuery.data.value ?? []"
          :notifications="[]"
          :busy="editorBusy || writesBlocked"
          @save="saveEditor"
          @cancel="closeEditor"
        />
      </div>
      <div
        v-else-if="!selectedId"
        class="flex-1 grid place-items-center text-sm chrome-text-muted"
      >
        {{ t('workflow.select_hint') }}
      </div>

      <template v-else>
        <!-- 详情头:名称 + 操作 -->
        <div class="flex items-center gap-2 px-4 py-2 border-b chrome-border-subtle chrome-bg-panel">
          <div class="flex items-center gap-2 text-section font-semibold chrome-text-heading min-w-0">
            <GitBranch class="w-4 h-4 shrink-0" />
            <span class="truncate">{{ detail?.name ?? '…' }}</span>
          </div>
          <div class="ml-auto flex items-center gap-2">
            <button
              type="button"
              class="chrome-btn-primary text-sm"
              :disabled="writesBlocked || !detail || !detail.enabled"
              :title="writesBlocked ? t('license.writes_blocked') : detail && !detail.enabled ? t('workflow.disabled') : ''"
              @click="openTrigger"
            >
              <Play class="w-3.5 h-3.5" /> {{ t('workflow.run') }}
            </button>
            <button
              type="button"
              class="chrome-btn-secondary text-sm"
              :disabled="writesBlocked || !detail"
              :title="writesBlocked ? t('license.writes_blocked') : ''"
              @click="openEdit"
            >
              {{ t('workflow.editor.edit_definition') }}
            </button>
            <button
              type="button"
              class="chrome-btn-ghost text-red-600 dark:text-red-400"
              :aria-label="t('workflow.delete')"
              :disabled="writesBlocked"
              :title="writesBlocked ? t('license.writes_blocked') : t('workflow.delete')"
              @click="deleteOpen = true"
            >
              <Trash2 class="w-4 h-4" />
            </button>
          </div>
        </div>

        <!-- tab 条 -->
        <div class="flex items-center gap-1 px-3 py-1.5 border-b chrome-border-subtle chrome-bg-panel">
          <button
            type="button"
            class="chrome-tab"
            :class="tab === 'detail' && 'chrome-accent-light-bg chrome-accent'"
            @click="tab = 'detail'"
          >
            <ListTree class="w-4 h-4" /> {{ t('workflow.tab_detail') }}
          </button>
          <button
            type="button"
            class="chrome-tab"
            :class="tab === 'runs' && 'chrome-accent-light-bg chrome-accent'"
            @click="tab = 'runs'"
          >
            <History class="w-4 h-4" /> {{ t('workflow.tab_runs') }}
          </button>
          <button
            type="button"
            class="chrome-tab"
            :class="tab === 'notify' && 'chrome-accent-light-bg chrome-accent'"
            @click="tab = 'notify'"
          >
            <Bell class="w-4 h-4" /> {{ t('workflow.tab_notify') }}
          </button>
        </div>

        <div class="flex-1 min-h-0 overflow-auto p-4">
          <WorkflowEditor
            v-if="editorIntent === 'edit'"
            :key="editorKey"
            :initial-name="detail?.name"
            :initial-enabled="detail?.enabled"
            :initial-spec="detail?.spec"
            :create-mode="false"
            :datasources="datasourceQuery.data.value ?? []"
            :compare-tasks="compareTasksQuery.data.value ?? []"
            :notifications="detail?.spec.notifications ?? []"
            :busy="editorBusy || writesBlocked"
            @save="saveEditor"
            @cancel="closeEditor"
          />
          <template v-else>
          <div v-if="detailQuery.isPending.value"><LoadingDots /></div>
          <div v-else-if="detailQuery.isError.value" class="text-sm text-red-600 dark:text-red-400">
            {{ workflowErrorMessage(detailQuery.error.value) }}
          </div>

          <template v-else-if="detail">
            <!-- ===== 详情 tab(只读)===== -->
            <div v-show="tab === 'detail'" class="space-y-4 max-w-4xl">
              <!-- 调度 -->
              <div class="rounded-card border chrome-border chrome-bg-panel p-3">
                <div class="text-xs font-medium chrome-text-heading mb-2">
                  {{ t('workflow.section_schedule') }}
                </div>
                <div v-if="detail.spec.schedule" class="text-sm chrome-text-normal">
                  <span class="font-mono">{{ detail.spec.schedule.cron }}</span>
                  <span class="ml-2 text-xs chrome-text-muted">
                    {{ detail.spec.schedule.enabled ? t('workflow.schedule_on') : t('workflow.schedule_off') }}
                  </span>
                </div>
                <div v-else class="text-sm chrome-text-muted">{{ t('workflow.no_schedule') }}</div>
              </div>

              <!-- 变量(C-7)-->
              <div class="rounded-card border chrome-border chrome-bg-panel p-3">
                <div class="text-xs font-medium chrome-text-heading mb-2">
                  {{ t('workflow.section_variables') }}
                </div>
                <div v-if="specVariables.length === 0" class="text-sm chrome-text-muted">
                  {{ t('workflow.no_variables') }}
                </div>
                <div v-else class="space-y-1">
                  <div
                    v-for="[name, value] in specVariables"
                    :key="name"
                    class="flex items-baseline gap-2 text-sm font-mono"
                  >
                    <span class="chrome-accent">{{ name }}</span>
                    <span class="chrome-text-muted">=</span>
                    <span class="chrome-text-normal break-all">
                      {{ Array.isArray(value) ? value.join(', ') : value }}
                    </span>
                  </div>
                </div>
              </div>

              <!-- 节点 -->
              <div class="rounded-card border chrome-border chrome-bg-panel p-3">
                <div class="text-xs font-medium chrome-text-heading mb-2">
                  {{ t('workflow.section_nodes', { count: detail.spec.nodes.length }) }}
                </div>
                <div class="space-y-2">
                  <div
                    v-for="node in detail.spec.nodes"
                    :key="node.id"
                    class="rounded-card border chrome-border px-3 py-2"
                  >
                    <div class="flex flex-wrap items-center gap-2">
                      <span class="text-sm font-mono chrome-text-heading">{{ node.id }}</span>
                      <span class="text-[11px] rounded-input px-1.5 py-0.5 chrome-bg-elevated chrome-text-muted font-mono">
                        {{ node.job_kind }}
                      </span>
                      <span class="text-[11px] chrome-text-muted">
                        {{ t('workflow.node_timeout', { s: node.timeout_seconds }) }}
                      </span>
                      <span class="text-[11px] chrome-text-muted">
                        {{ t('workflow.node_on_failure') }}: {{ node.on_failure }}
                      </span>
                      <span
                        v-if="node.retry_policy"
                        class="text-[11px] chrome-text-muted"
                      >
                        {{ t('workflow.node_retry', { n: node.retry_policy.max_retries, b: node.retry_policy.backoff_seconds }) }}
                      </span>
                    </div>
                    <div v-if="node.when" class="mt-1 text-[11px] chrome-text-muted font-mono">
                      when: {{ node.when }}
                    </div>
                    <div class="mt-1 text-[11px] chrome-text-muted font-mono break-all">
                      payload: {{ payloadPreview(node.payload) }}
                    </div>
                  </div>
                </div>
              </div>

              <!-- 边 -->
              <div class="rounded-card border chrome-border chrome-bg-panel p-3">
                <div class="text-xs font-medium chrome-text-heading mb-2">
                  {{ t('workflow.section_edges', { count: detail.spec.edges.length }) }}
                </div>
                <div v-if="detail.spec.edges.length === 0" class="text-sm chrome-text-muted">
                  {{ t('workflow.no_edges') }}
                </div>
                <div v-else class="space-y-1">
                  <div
                    v-for="(edge, i) in detail.spec.edges"
                    :key="i"
                    class="text-sm font-mono chrome-text-normal"
                  >
                    {{ edge.source }} → {{ edge.target }}
                  </div>
                </div>
              </div>
            </div>

            <!-- ===== run 历史 tab ===== -->
            <div v-show="tab === 'runs'" class="flex gap-4 min-h-0">
              <!-- 历史列表 -->
              <div class="w-64 shrink-0 space-y-1">
                <div class="text-xs font-medium chrome-text-heading mb-1">
                  {{ t('workflow.run_history') }}
                </div>
                <div v-if="runsQuery.isPending.value"><LoadingDots /></div>
                <div v-else-if="runs.length === 0" class="text-xs chrome-text-muted">
                  {{ t('workflow.no_runs') }}
                </div>
                <button
                  v-for="r in runs"
                  :key="r.run_id"
                  type="button"
                  :data-testid="`workflow-run-${r.run_id}`"
                  class="w-full text-left rounded-card border px-2 py-1.5 transition-colors"
                  :class="
                    r.run_id === selectedRunId
                      ? 'chrome-accent-light-bg border-transparent'
                      : 'chrome-border hover:chrome-bg-elevated'
                  "
                  @click="openRun(r.run_id)"
                >
                  <div class="flex items-center justify-between gap-2">
                    <JobStatusBadge :status="r.status" />
                    <span class="text-[10px] chrome-text-muted font-mono truncate">
                      {{ r.run_id.slice(0, 8) }}
                    </span>
                  </div>
                  <div class="mt-1 text-[11px] chrome-text-muted">{{ fmtTime(r.created_at) }}</div>
                </button>
                <div class="flex items-center justify-between gap-2 pt-2">
                  <button
                    type="button"
                    class="chrome-btn-secondary text-xs"
                    :disabled="runOffset === 0"
                    :aria-label="t('workflow.previous_page')"
                    @click="previousRunPage"
                  >
                    {{ t('workflow.previous_page') }}
                  </button>
                  <span class="text-[10px] chrome-text-muted">
                    {{ Math.floor(runOffset / RUN_PAGE_SIZE) + 1 }}
                  </span>
                  <button
                    type="button"
                    class="chrome-btn-secondary text-xs"
                    :disabled="!runsQuery.data.value?.has_more"
                    :aria-label="t('workflow.next_page')"
                    @click="nextRunPage"
                  >
                    {{ t('workflow.next_page') }}
                  </button>
                </div>
              </div>

              <!-- 单 run 状态 -->
              <div class="flex-1 min-w-0">
                <div v-if="!selectedRunId" class="text-sm chrome-text-muted">
                  {{ t('workflow.run_select_hint') }}
                </div>
                <template v-else>
                  <div v-if="runStatusQuery.isPending.value"><LoadingDots /></div>
                  <template v-else-if="runStatus">
                    <div class="flex items-center gap-3 mb-3">
                      <JobStatusBadge :status="runStatus.status" size="md" />
                      <span class="text-xs chrome-text-muted font-mono">{{ runStatus.run_id }}</span>
                      <button
                        v-if="!runIsTerminal"
                        type="button"
                        class="ml-auto chrome-btn-secondary text-xs"
                        :disabled="writesBlocked || cancelBusy"
                        :title="writesBlocked ? t('license.writes_blocked') : ''"
                        @click="cancelRun"
                      >
                        <X class="w-3.5 h-3.5" /> {{ t('workflow.cancel_run') }}
                      </button>
                    </div>
                    <div
                      v-if="runStatus.error"
                      class="mb-3 rounded-card border border-red-300 dark:border-red-500/40 bg-red-50 dark:bg-red-500/10 p-2 text-xs text-red-700 dark:text-red-300 break-all"
                    >
                      {{ runStatus.error }}
                    </div>
                    <div class="rounded-card border chrome-border overflow-hidden">
                      <table class="w-full text-sm">
                        <thead class="chrome-bg-elevated text-[11px] chrome-text-muted">
                          <tr>
                            <th class="text-left px-3 py-1.5 font-medium">{{ t('workflow.col_node') }}</th>
                            <th class="text-left px-3 py-1.5 font-medium">{{ t('workflow.col_kind') }}</th>
                            <th class="text-left px-3 py-1.5 font-medium">{{ t('workflow.col_status') }}</th>
                            <th class="text-left px-3 py-1.5 font-medium">{{ t('workflow.col_attempts') }}</th>
                            <th class="text-left px-3 py-1.5 font-medium">{{ t('workflow.col_outputs') }}</th>
                          </tr>
                        </thead>
                        <tbody>
                          <tr
                            v-for="node in runStatus.nodes"
                            :key="node.node_id"
                            class="border-t chrome-border-subtle"
                          >
                            <td class="px-3 py-1.5 font-mono chrome-text-heading">{{ node.node_id }}</td>
                            <td class="px-3 py-1.5 font-mono chrome-text-muted">{{ node.job_kind }}</td>
                            <td class="px-3 py-1.5">
                              <span
                                class="inline-flex items-center rounded-input px-1.5 py-0.5 text-[11px] font-medium"
                                :class="nodeStatusClass(node.status)"
                              >
                                {{ nodeStatusLabel(node.status) }}
                              </span>
                              <div v-if="node.error" class="mt-0.5 text-[10px] text-red-600 dark:text-red-400 break-all">
                                {{ node.error }}
                              </div>
                            </td>
                            <td class="px-3 py-1.5 tabular-nums chrome-text-muted">{{ node.attempts }}</td>
                            <td class="px-3 py-1.5">
                              <div
                                v-for="[key, value] in safeOutputEntries(node.outputs)"
                                :key="key"
                                class="flex items-center gap-2 text-[10px]"
                              >
                                <span class="font-mono chrome-text-muted">{{ key }}</span>
                                <span class="font-mono chrome-text-normal">{{ outputValue(value) }}</span>
                              </div>
                              <span
                                v-if="safeOutputEntries(node.outputs).length === 0"
                                class="text-[10px] chrome-text-muted"
                              >
                                —
                              </span>
                            </td>
                          </tr>
                        </tbody>
                      </table>
                    </div>
                  </template>
                  <div v-else-if="runStatusQuery.isError.value" class="text-sm text-red-600 dark:text-red-400">
                    {{ workflowErrorMessage(runStatusQuery.error.value) }}
                  </div>
                </template>
              </div>
            </div>

            <!-- ===== 通知 tab ===== -->
            <div v-show="tab === 'notify'" class="space-y-3 max-w-3xl">
              <div class="flex items-center justify-between">
                <div class="text-xs font-medium chrome-text-heading">
                  {{ t('workflow.notify_title') }}
                </div>
                <button
                  type="button"
                  class="chrome-btn-primary text-xs"
                  :disabled="writesBlocked"
                  :title="writesBlocked ? t('license.writes_blocked') : ''"
                  @click="openNotifyCreate"
                >
                  <Plus class="w-3.5 h-3.5" /> {{ t('workflow.notify_add') }}
                </button>
              </div>
              <div class="flex items-start gap-2 rounded-card border chrome-border chrome-bg-panel p-2 text-[11px] chrome-text-muted">
                <Info class="w-3.5 h-3.5 shrink-0 mt-0.5" /> {{ t('workflow.notify_url_hint') }}
              </div>
              <div v-if="notifyTargets.length === 0" class="text-sm chrome-text-muted">
                {{ t('workflow.no_notify') }}
              </div>
              <div
                v-for="target in notifyTargets"
                :key="target.id"
                class="flex flex-wrap items-center gap-2 rounded-card border chrome-border px-3 py-2"
              >
                <span class="text-sm font-medium chrome-text-heading">{{ target.channel }}</span>
                <span class="text-[11px] chrome-text-muted">
                  {{ t('workflow.notify_events') }}: {{ (target.events ?? []).join(', ') || t('workflow.notify_default_events') }}
                </span>
                <span v-if="!target.enabled" class="text-[11px] text-amber-600 dark:text-amber-400">
                  {{ t('workflow.disabled') }}
                </span>
                <span class="text-[11px] chrome-text-muted">{{ target.timeout_seconds }}s</span>
                <div class="ml-auto flex items-center gap-1">
                  <button
                    type="button"
                    class="chrome-btn-secondary text-xs"
                    :disabled="writesBlocked"
                    :title="writesBlocked ? t('license.writes_blocked') : ''"
                    @click="openNotifyEdit(target)"
                  >
                    {{ t('common.edit') }}
                  </button>
                  <button
                    type="button"
                    class="chrome-btn-ghost text-red-600 dark:text-red-400"
                    :aria-label="t('workflow.delete')"
                    :disabled="writesBlocked"
                    :title="writesBlocked ? t('license.writes_blocked') : t('workflow.delete')"
                    @click="notifyDeleteId = target.id"
                  >
                    <Trash2 class="w-4 h-4" />
                  </button>
                </div>
              </div>
            </div>
          </template>
          </template>
        </div>
      </template>
    </div>

    <!-- ============ 删除确认 modal ============ -->
    <Modal :open="deleteOpen" :title="t('workflow.delete_title')" @close="deleteOpen = false">
      <div class="space-y-4">
        <p class="text-sm chrome-text-normal">
          {{ t('workflow.delete_confirm', { name: detail?.name ?? '' }) }}
        </p>
        <div class="flex justify-end gap-2">
          <button type="button" class="chrome-btn-secondary text-sm" @click="deleteOpen = false">
            {{ t('common.cancel') }}
          </button>
          <button
            type="button"
            class="chrome-btn-danger text-sm"
            :disabled="writesBlocked || deleteBusy"
            :title="writesBlocked ? t('license.writes_blocked') : ''"
            @click="confirmDelete"
          >
            {{ t('workflow.delete') }}
          </button>
        </div>
      </div>
    </Modal>

    <!-- ============ 触发 run modal(C-7 变量)============ -->
    <Modal :open="triggerOpen" :title="t('workflow.trigger_title')" @close="triggerOpen = false">
      <div class="space-y-3">
        <p class="text-xs chrome-text-muted">{{ t('workflow.trigger_hint') }}</p>
        <div class="space-y-2">
          <div v-for="(row, i) in triggerVars" :key="i" class="flex items-center gap-2">
            <input
              v-model="row.name"
              class="chrome-input flex-1 text-sm font-mono"
              :placeholder="t('workflow.var_name_ph')"
            />
            <input
              v-model="row.value"
              class="chrome-input flex-1 text-sm font-mono"
              :placeholder="t('workflow.var_value_ph')"
            />
            <button
              type="button"
              class="chrome-btn-ghost"
              :aria-label="t('workflow.remove_variable')"
              @click="removeVarRow(i)"
            >
              <X class="w-4 h-4" />
            </button>
          </div>
        </div>
        <button type="button" class="chrome-btn-secondary text-xs" @click="addVarRow">
          <Plus class="w-3.5 h-3.5" /> {{ t('workflow.add_variable') }}
        </button>
        <p class="text-[11px] chrome-text-muted">{{ t('workflow.var_charset_hint') }}</p>
        <div v-if="triggerError" class="text-xs text-red-600 dark:text-red-400">{{ triggerError }}</div>
        <div class="flex justify-end gap-2">
          <button type="button" class="chrome-btn-secondary text-sm" @click="triggerOpen = false">
            {{ t('common.cancel') }}
          </button>
          <button
            type="button"
            class="chrome-btn-primary text-sm"
            :disabled="writesBlocked || triggerBusy"
            :title="writesBlocked ? t('license.writes_blocked') : ''"
            @click="submitTrigger"
          >
            <Play class="w-3.5 h-3.5" /> {{ t('workflow.run') }}
          </button>
        </div>
      </div>
    </Modal>

    <!-- ============ 通知目标 modal ============ -->
    <Modal
      :open="notifyOpen"
      :title="notifyForm.targetId ? t('workflow.notify_edit_title') : t('workflow.notify_add_title')"
      @close="closeNotify"
    >
      <div class="space-y-3">
        <label class="block" v-if="!notifyForm.targetId">
          <span class="block text-xs chrome-text-muted mb-1">{{ t('workflow.notify_channel') }}</span>
          <select v-model="notifyForm.channel" class="chrome-input w-full text-sm">
            <option value="webhook">webhook</option>
            <option value="wecom">wecom</option>
            <option value="email">email</option>
          </select>
        </label>
        <label v-if="notifyForm.channel !== 'email'" class="block">
          <span class="block text-xs chrome-text-muted mb-1">
            {{ t('workflow.notify_url') }}
            <span v-if="notifyForm.targetId" class="chrome-text-muted">({{ t('workflow.notify_url_edit_hint') }})</span>
          </span>
          <input
            v-model="notifyForm.url"
            type="url"
            autocomplete="off"
            class="chrome-input w-full text-sm font-mono"
            :placeholder="notifyForm.targetId ? t('workflow.notify_url_keep_ph') : t('workflow.notify_url_ph')"
          />
        </label>
        <div v-else class="grid gap-3 sm:grid-cols-2">
          <label class="block">
            <span class="block text-xs chrome-text-muted mb-1">{{ t('workflow.notify_smtp_host') }}</span>
            <input v-model="notifyForm.smtp_host" class="chrome-input w-full text-sm" />
          </label>
          <label class="block">
            <span class="block text-xs chrome-text-muted mb-1">{{ t('workflow.notify_smtp_port') }}</span>
            <input
              v-model.number="notifyForm.smtp_port"
              type="number"
              min="1"
              max="65535"
              class="chrome-input w-full text-sm"
            />
          </label>
          <label class="block">
            <span class="block text-xs chrome-text-muted mb-1">{{ t('workflow.notify_smtp_from') }}</span>
            <input v-model="notifyForm.smtp_from" type="email" class="chrome-input w-full text-sm" />
          </label>
          <label class="block">
            <span class="block text-xs chrome-text-muted mb-1">{{ t('workflow.notify_smtp_to') }}</span>
            <input
              v-model="notifyForm.smtp_to"
              class="chrome-input w-full text-sm"
              :placeholder="t('workflow.notify_smtp_to_ph')"
            />
          </label>
          <label class="block">
            <span class="block text-xs chrome-text-muted mb-1">{{ t('workflow.notify_smtp_user') }}</span>
            <input v-model="notifyForm.smtp_user" autocomplete="off" class="chrome-input w-full text-sm" />
          </label>
          <label class="block">
            <span class="block text-xs chrome-text-muted mb-1">
              {{ t('workflow.notify_smtp_password') }}
              <span v-if="notifyForm.targetId">({{ t('workflow.notify_password_edit_hint') }})</span>
            </span>
            <input
              v-model="notifyForm.smtp_password"
              type="password"
              autocomplete="new-password"
              class="chrome-input w-full text-sm"
            />
          </label>
        </div>
        <fieldset>
          <legend class="block text-xs chrome-text-muted mb-1">{{ t('workflow.notify_events') }}</legend>
          <div class="flex flex-wrap gap-2">
            <label
              v-for="event in NOTIFY_EVENTS"
              :key="event"
              class="flex items-center gap-1.5 rounded-input border chrome-border-subtle px-2 py-1 text-xs chrome-text-normal"
            >
              <input
                type="checkbox"
                :checked="notifyForm.events.includes(event)"
                @change="toggleNotifyEvent(event, ($event.target as HTMLInputElement).checked)"
              />
              {{ event }}
            </label>
          </div>
        </fieldset>
        <div class="flex items-center gap-4">
          <label class="flex items-center gap-2 text-xs chrome-text-normal">
            <input v-model="notifyForm.enabled" type="checkbox" /> {{ t('workflow.notify_enabled') }}
          </label>
          <label class="flex items-center gap-2 text-xs chrome-text-muted">
            {{ t('workflow.notify_timeout') }}
            <input
              v-model.number="notifyForm.timeout_seconds"
              type="number"
              min="1"
              max="60"
              class="chrome-input w-20 text-sm"
            />
          </label>
        </div>
        <div v-if="notifyError" class="text-xs text-red-600 dark:text-red-400">{{ notifyError }}</div>
        <div class="flex justify-end gap-2">
          <button type="button" class="chrome-btn-secondary text-sm" @click="closeNotify">
            {{ t('common.cancel') }}
          </button>
          <button
            type="button"
            class="chrome-btn-primary text-sm"
            :disabled="writesBlocked || notifyBusy"
            :title="writesBlocked ? t('license.writes_blocked') : ''"
            @click="submitNotify"
          >
            {{ t('common.save') }}
          </button>
        </div>
      </div>
    </Modal>

    <!-- ============ 删除通知确认 modal ============ -->
    <Modal :open="notifyDeleteId !== null" :title="t('workflow.notify_delete_title')" @close="notifyDeleteId = null">
      <div class="space-y-4">
        <p class="text-sm chrome-text-normal">{{ t('workflow.notify_delete_confirm') }}</p>
        <div class="flex justify-end gap-2">
          <button type="button" class="chrome-btn-secondary text-sm" @click="notifyDeleteId = null">
            {{ t('common.cancel') }}
          </button>
          <button
            type="button"
            class="chrome-btn-danger text-sm"
            :disabled="writesBlocked"
            :title="writesBlocked ? t('license.writes_blocked') : ''"
            @click="confirmDeleteNotify"
          >
            {{ t('workflow.delete') }}
          </button>
        </div>
      </div>
    </Modal>
  </div>
</template>
