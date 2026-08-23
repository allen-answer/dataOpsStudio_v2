<script setup lang="ts">
/**
 * SqlWorkspaceView —— /projects/:id/sql
 *
 * 2.1.0 SQL Workspace:
 * - 多 console 持久化与防抖保存
 * - SQL 历史从 jobs 推导
 * - SQL 模板浏览 / 变量渲染 / admin CRUD
 * - running 状态轮询 ResultSet,展示已 spool 行
 */
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useQuery } from '@tanstack/vue-query'
import { storeToRefs } from 'pinia'
import {
  AlertTriangle,
  AlignLeft,
  Asterisk,
  BookOpen,
  ChevronDown,
  ChevronRight,
  Database,
  Download,
  FileText,
  Gauge,
  History,
  Info,
  Key,
  ListTree,
  Network,
  Pencil,
  Pin,
  PinOff,
  Play,
  Plus,
  RefreshCw,
  Save,
  Search,
  ShieldCheck,
  Sparkles,
  Square,
  Table2,
  Trash2,
  X,
} from 'lucide-vue-next'
import type * as monaco from 'monaco-editor/esm/vs/editor/editor.api'
import { listDatasources } from '../api/datasources'
import {
  createSqlConsole,
  createSqlTemplate,
  deleteSqlConsole,
  deleteSqlTemplate,
  executeSql,
  listSqlConsoles,
  listSqlHistory,
  listSqlTemplates,
  renderSqlTemplate,
  updateSqlConsole,
  updateSqlTemplate,
  type SqlConsole,
  type SqlConsoleUpdateRequest,
  type SqlHistoryItem,
  type SqlTemplate,
  type SqlTemplateCreateRequest,
} from '../api/sql'
import {
  cancelJob,
  getJob,
  getJobProgress,
  getJobResult,
  requestJobPage,
  type JobResponse,
  type JobProgressResponse,
  type JobResultResponse,
  type JobStageTimings,
} from '../api/jobs'
import {
  createExport,
  downloadExport,
  expandSqlStar,
  formatSql,
  listMetadataColumns,
  listMetadataSchemas,
  listMetadataTables,
  type MetadataColumnItem,
  type MetadataSchemaItem,
  type MetadataTableItem,
} from '../api/metadata'
import { explainSql, preflightSql, type SqlPreflightFinding } from '../api/sql'
import {
  cancelStatement,
  createStatementExport,
  getStatementProgress,
  getStatementResult,
  submitStatement,
  toJobProgress,
  toJobResult,
} from '../api/sessions'
import {
  useConsoleSessions,
  type ConsoleSessionState,
} from '../composables/useConsoleSessions'
import ConsoleSessionBar from '../components/ConsoleSessionBar.vue'
import { diagnoseSlowSql } from '../api/ai'
import { ApiError, type DatasourceListItem, type ExportFormat, type JobStatus } from '../api/types'
import { useThemeStore } from '../stores/theme'
import { useAuthStore } from '../stores/auth'
import JobStatusBadge from '../components/JobStatusBadge.vue'
import ResultTable from '../components/ResultTable.vue'
import LoadingDots from '../components/LoadingDots.vue'
import Modal from '../components/Modal.vue'
import AiSqlAssistantPanel from '../components/AiSqlAssistantPanel.vue'
import SqlEditor from '../components/SqlEditor.vue'
import { useLicense } from '../composables/useLicense'
import { clearSqlMetadataCache } from '../utils/sqlIntelligence'
import { splitSqlStatements, statementAtOffset } from '../utils/sqlStatements'
import { createUserErrorMessage } from '../utils/userErrorMessage'

const TERMINAL: ReadonlySet<JobStatus> = new Set<JobStatus>([
  'success',
  'failed',
  'cancelled',
  'timeout',
])
const ACTIVE: ReadonlySet<JobStatus> = new Set<JobStatus>(['pending', 'running'])
const DEFAULT_PAGE_SIZE = 100
const PAGE_SIZE_OPTIONS = [50, 100, 200, 500] as const
const DEFAULT_QUERY_MAX_ROWS = 1_000
const QUERY_MAX_ROWS_LIMIT = 50_000
const QUERY_MAX_ROWS_OPTIONS = [100, 500, 1_000, 5_000, 10_000] as const
const POLL_MS = 1000
const QUERY_POLL_BACKOFF_MS = [1000, 2000, 3000, 5000] as const
const QUERY_POLL_HIDDEN_MS = 5000
const QUERY_POLL_LEASE_MS = 15000
const QUERY_POLL_LEASE_PREFIX = 'dataops-sql-progress-lease:'
const SAVE_DEBOUNCE_MS = 650
// db2 后端 adapter 已具备执行能力,但 GA 决策维持 Preview,放开执行需单独 PR 人拍板。
const SUPPORTED_EXECUTION_DB_TYPES = new Set(['mysql', 'dm', 'postgresql', 'db2'])
// EXPLAIN / expand-star 依赖 sqlglot 方言 + 元数据缓存,与执行同口径(mysql / dm / postgresql)。
const SUPPORTED_TOOL_DB_TYPES = new Set(['mysql', 'dm', 'postgresql'])
const EXPORT_FORMATS: ExportFormat[] = ['csv', 'excel', 'json', 'sql']

type SidebarTab = 'consoles' | 'history' | 'templates' | 'metadata'
type HistoryRange = 'all' | 'today' | '7d'
type ResultTab = 'result' | 'plan' | 'stats'

interface ConsoleRuntime {
  statement: string
  statementIndex: number
  statementCount: number
  jobId: string | null
  rootJobId: string | null
  // 会话路径的语句身份(Session Broker)。与 jobId 互斥:一个 runtime 要么是
  // 一条 job,要么是一条会话语句;`runtimeKey` 统一取身份键喂轮询机器。
  statementId: string | null
  // 幂等回执键:提交请求丢响应时可安全重发**提交**而不会二次执行(设计 §3.1)。
  clientRequestId: string | null
  // 提交时持有的 epoch;取消要带它,过期即 409 stale_session_epoch(M8)。
  statementEpoch: number
  resultSetId: string | null
  status: JobStatus | null
  error: string | null
  message: string | null
  cancelling: boolean
  result: JobResultResponse | null
  resultLoading: boolean
  pageOffset: number
  pageSize: number
  startedAt: number | null
  finishedAt: number | null
  timings: JobStageTimings | null
  progress: JobProgressResponse | null
  resultVersion: number
  // Guards result reads that outlive a page switch, console switch, or abort.
  resultGeneration: number
  unchangedPolls: number
  networkFailures: number
  // EXPLAIN 计划态(独立于普通查询,落 Plan tab)
  planJobId: string | null
  planStatus: JobStatus | null
  planResult: JobResultResponse | null
  planError: string | null
  resultTab: ResultTab
}

interface ResultReadOutcome {
  applied: boolean
  generation: number
}

// 元数据树节点态:数据源 -> schema -> 表 -> 列
interface MetadataTableNode {
  table: MetadataTableItem
  expanded: boolean
  loading: boolean
  error: string | null
  columns: MetadataColumnItem[]
}

interface MetadataSchemaNode {
  schema: MetadataSchemaItem
  expanded: boolean
  loading: boolean
  error: string | null
  tables: MetadataTableNode[]
}

interface TemplateForm {
  id: string | null
  name: string
  description: string
  category: string
  sql_text: string
  variables: string
  project_id: string
}

const { t, te } = useI18n()
const errorMessage = createUserErrorMessage(t)
const route = useRoute()
const { writesBlocked } = useLicense()
const themeStore = useThemeStore()
const authStore = useAuthStore()
const { variant } = storeToRefs(themeStore)

const projectId = computed(() =>
  typeof route.params.id === 'string' ? route.params.id : '',
)
const isAdmin = computed(() => authStore.user?.role === 'admin')

const dsQuery = useQuery({
  queryKey: computed(() => ['datasources', projectId.value]),
  queryFn: () => listDatasources(projectId.value),
  enabled: computed(() => Boolean(projectId.value)),
})
const datasources = computed<DatasourceListItem[]>(() => dsQuery.data.value ?? [])

const consoles = ref<SqlConsole[]>([])
const consolesLoading = ref(false)
const consoleError = ref<string | null>(null)
const activeConsoleId = ref<string | null>(null)
const sidebarTab = ref<SidebarTab>('consoles')

const editorSql = ref('SELECT 1 AS hello;')
const selectedDsId = ref('')
const pageSizeSelection = ref(DEFAULT_PAGE_SIZE)
const maxRowsSelection = ref(String(DEFAULT_QUERY_MAX_ROWS))
const customMaxRows = ref(DEFAULT_QUERY_MAX_ROWS)
const suppressConsoleSave = ref(false)

const runtimes = reactive<Record<string, ConsoleRuntime[]>>({})
const activeRuntimeIndexes = reactive<Record<string, number>>({})
const pollTimers = new Map<string, ReturnType<typeof setTimeout>>()
const pollControllers = new Map<string, AbortController>()
const saveTimers = new Map<string, ReturnType<typeof setTimeout>>()
const pendingConsolePatches = new Map<string, SqlConsoleUpdateRequest>()
const nowMs = ref(Date.now())
let nowTimer: ReturnType<typeof setInterval> | null = null
let sqlEditor: monaco.editor.IStandaloneCodeEditor | null = null
const pollingTabId = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`
let progressChannel: BroadcastChannel | null = null

const historyItems = ref<SqlHistoryItem[]>([])
const historyLoading = ref(false)
const historyError = ref<string | null>(null)
const historyDatasourceId = ref('')
const historyRange = ref<HistoryRange>('all')

const templates = ref<SqlTemplate[]>([])
const templatesLoading = ref(false)
const templatesError = ref<string | null>(null)
const templateSearch = ref('')
const selectedTemplate = ref<SqlTemplate | null>(null)
const templateValues = reactive<Record<string, string>>({})
const templateRenderError = ref<string | null>(null)
const templateModalOpen = ref(false)
const templateSaving = ref(false)
const templateForm = reactive<TemplateForm>({
  id: null,
  name: '',
  description: '',
  category: 'general',
  sql_text: 'SELECT * FROM {{table_name}} LIMIT 100',
  variables: 'table_name',
  project_id: '',
})

const renameConsoleId = ref<string | null>(null)
const renameConsoleDraft = ref('')
const execError = ref<string | null>(null)
const resultPanel = ref<HTMLElement | null>(null)

// ── 控制台会话(Session Broker,设计 §3.3)──────────────────────────
// 懒 attach:这里只持有绑定态,首次执行才建会话;方言不支持或部署开关关闭时
// `ensureSession` 返回 null,整段执行路径原样回落到 job 路径。
const consoleSessions = useConsoleSessions()
const sessionBusy = ref(false)

// ── 元数据浏览器(左侧第四 tab)─────────────────────────────────────
const metadataDsId = ref('')
const metadataSchemas = ref<MetadataSchemaNode[]>([])
const metadataLoading = ref(false)
const metadataError = ref<string | null>(null)

// ── 编辑器工具栏(format / expand-star / explain / preflight)──────
const toolError = ref<string | null>(null)
const toolBusy = ref<'' | 'format' | 'expand' | 'explain' | 'preflight'>('')

// SQL 体检(C-11):文本级 advisory finding;卡片展示,可关闭。行数估算复用 EXPLAIN。
const preflightFindings = ref<SqlPreflightFinding[] | null>(null)

// ── AI 生成(C1:候选表确认 → 预览 → 用户应用到编辑器)──────────────
const aiPanelOpen = ref(false)

// ── AI 慢 SQL 根因诊断(C4:EXPLAIN + 结构 + 历史基线 → 根因排序,egress L3)──
const aiDiagnoseBusy = ref(false)
const aiDiagnoseError = ref<string | null>(null)
const aiDiagnoseDisabled = ref(false)
const aiDiagnosis = ref<string | null>(null)
const aiDiagnoseBaseline = ref<{ available: boolean; runs: number } | null>(null)

// ── 导出(4 格式 → 一次性 token → 下载)────────────────────────────
const exportMenuOpen = ref(false)
const exportBusy = ref(false)
const exportError = ref<string | null>(null)
const exportReady = ref<{ token: string; filename: string } | null>(null)
const exportPollTimers = new Set<ReturnType<typeof setTimeout>>()

const activeConsole = computed<SqlConsole | null>(
  () => consoles.value.find((item) => item.id === activeConsoleId.value) ?? null,
)
const activeRuntimes = computed<ConsoleRuntime[]>(() =>
  activeConsoleId.value ? runtimesFor(activeConsoleId.value) : [],
)
const activeRuntime = computed<ConsoleRuntime | null>(() => {
  if (!activeConsoleId.value) return null
  const items = activeRuntimes.value
  return items[activeRuntimeIndexes[activeConsoleId.value] ?? 0] ?? items[0] ?? null
})
const selectedDs = computed<DatasourceListItem | undefined>(() =>
  datasources.value.find((d) => d.id === selectedDsId.value),
)
const unsupportedDb = computed<string | null>(() => {
  const ds = selectedDs.value
  return ds && !SUPPORTED_EXECUTION_DB_TYPES.has(ds.db_type) ? ds.db_type : null
})
const queryMaxRows = computed<number | null>(() => {
  const value =
    maxRowsSelection.value === 'custom'
      ? Number(customMaxRows.value)
      : Number(maxRowsSelection.value)
  if (!Number.isInteger(value) || value < 1 || value > QUERY_MAX_ROWS_LIMIT) return null
  return value
})
// 工具能力门:db_type 在白名单内 + (EXPLAIN 还需 operation_policy.allow_explain)。
const toolsSupported = computed<boolean>(() => {
  const ds = selectedDs.value
  return Boolean(ds && SUPPORTED_TOOL_DB_TYPES.has(ds.db_type))
})
const explainSupported = computed<boolean>(
  () => toolsSupported.value && Boolean(selectedDs.value?.operation_policy.allow_explain),
)
const metadataDs = computed<DatasourceListItem | undefined>(() =>
  datasources.value.find((d) => d.id === metadataDsId.value),
)
const metadataToolsSupported = computed<boolean>(() => {
  const ds = metadataDs.value
  return Boolean(ds && SUPPORTED_TOOL_DB_TYPES.has(ds.db_type))
})
/**
 * 可导出源。job 路径仅 success(后端 _require_exportable_source 只认 SQL_QUERY 且
 * success);会话路径追加 cancelled / timeout —— 取消保留的部分结果可读即可导出
 * (设计 §2.2 / F6),这正是与 job 路径「取消即删 spool」的刻意差别。
 */
const EXPORTABLE_STATEMENT_STATUSES: ReadonlySet<JobStatus> = new Set<JobStatus>([
  'success',
  'cancelled',
  'timeout',
])
const exportableSource = computed<{ kind: 'job' | 'statement'; id: string } | null>(() => {
  const rt = activeRuntime.value
  if (!rt || !rt.status) return null
  if (rt.statementId) {
    return EXPORTABLE_STATEMENT_STATUSES.has(rt.status) && rt.resultSetId
      ? { kind: 'statement', id: rt.statementId }
      : null
  }
  return rt.status === 'success' && rt.jobId ? { kind: 'job', id: rt.jobId } : null
})

// ── 会话状态条 / banner 的渲染判据 ──────────────────────────────
const activeSession = computed(() => consoleSessions.sessionFor(activeConsoleId.value))
const sessionTakenOver = computed(() => consoleSessions.isTakenOver(activeConsoleId.value))
const sessionLost = computed(() => consoleSessions.isLost(activeConsoleId.value))
const editorTheme = computed(() => {
  const v = variant.value
  return v === 'spotify-dark' || v === 'figma-dark' ? 'vs-dark' : 'vs'
})
const editorReadOnly = computed(() => {
  return activeRuntimes.value.some((runtime) => runtime.status && ACTIVE.has(runtime.status))
})
const sortedConsoles = computed(() =>
  [...consoles.value].sort((a, b) => {
    if (a.pinned !== b.pinned) return a.pinned ? -1 : 1
    return new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime()
  }),
)
const templateCategories = computed(() => {
  const out = new Map<string, SqlTemplate[]>()
  for (const template of templates.value) {
    const list = out.get(template.category) ?? []
    list.push(template)
    out.set(template.category, list)
  }
  return [...out.entries()]
})
const statusSummary = computed(() => {
  const rt = activeRuntime.value
  if (!rt?.status) return t('sql.run_to_see_result')
  const loaded = rt.result?.loaded_rows ?? 0
  if (rt.status === 'pending') return t('sql.status_pending')
  if (rt.status === 'running') {
    return t('sql.status_running_loaded', {
      seconds: elapsedSeconds(rt),
      rows: loaded,
    })
  }
  if (rt.status === 'success') {
    const rows = rt.result?.total_rows ?? rt.result?.loaded_rows ?? loaded
    return t('sql.status_success_summary', {
      rows: rt.result?.has_more ? `${rows}+` : rows,
      seconds: elapsedSeconds(rt),
    })
  }
  if (rt.status === 'cancelled') return t('sql.cancelled_hint')
  if (rt.status === 'timeout') return t('sql.timeout_hint')
  return rt.error || t('jobs.error.sql_failed')
})
/**
 * 会话路径取消 / 超时会**保留已落 spool 的行**(设计 §2.2/§11-7,与 job 路径
 * 「取消即删」刻意不同)。有行就明说「这是被打断前的部分结果」,不让用户把
 * 半张表当成完整结果读。
 */
const sessionPartialHint = computed<string | null>(() => {
  const rt = activeRuntime.value
  if (!rt?.statementId || !rt.status) return null
  if (rt.status !== 'cancelled' && rt.status !== 'timeout') return null
  const rows = rt.result?.loaded_rows ?? rt.result?.rows.length ?? 0
  return rows > 0 ? t('sql.session_partial_rows', { rows }) : null
})
const resultPanelError = computed<string | null>(() => {
  if (execError.value) return execError.value
  const runtime = activeRuntime.value
  if (!runtime || runtime.status === 'cancelled') return null
  return runtime.error
})
const shouldShowResultTable = computed(() => {
  const rt = activeRuntime.value
  if (!rt?.result) return false
  if (rt.status && TERMINAL.has(rt.status)) return true
  return rt.result.rows.length > 0 || rt.result.columns.length > 0
})
const limitScanWarning = computed(() => {
  const execution = activeRuntime.value?.progress?.execution
  return execution?.output_limit_applied === true && execution.limit_pushdown === false
})
const planActive = computed<boolean>(() => {
  const s = activeRuntime.value?.planStatus
  return s ? ACTIVE.has(s) : false
})
// Stats tab:从已加载的 ResultSet manifest 字段汇总(行数 / 截断 / 耗时 / 列数)。
interface StatRow {
  label: string
  value: string
}
const statRows = computed<StatRow[]>(() => {
  const rt = activeRuntime.value
  if (!rt) return []
  const res = rt.result
  const rows: StatRow[] = []
  rows.push({ label: t('sql.stats_status'), value: rt.status ?? '-' })
  rows.push({ label: t('sql.stats_elapsed'), value: `${elapsedSeconds(rt)}s` })
  rows.push({ label: t('sql.page_size'), value: String(rt.pageSize) })
  rows.push({
    label: t('sql.max_rows'),
    value:
      res?.max_result_rows != null
        ? String(res.max_result_rows)
        : rt.progress?.execution?.max_rows != null
          ? String(rt.progress.execution.max_rows)
          : '-',
  })
  rows.push({
    label: t('sql.stats_has_more'),
    value:
      res?.has_more == null
        ? '-'
        : res.has_more
          ? t('sql.stats_yes')
          : t('sql.stats_no'),
  })
  rows.push({
    label: t('sql.stats_loaded_rows'),
    value: res?.loaded_rows != null ? String(res.loaded_rows) : '-',
  })
  for (const [labelKey, timingKey] of [
    ['sql.stats_queue', 'queue_ms'],
    ['sql.stats_connect', 'connect_ms'],
    ['sql.stats_execute_first_row', 'execute_first_row_ms'],
    ['sql.stats_fetch', 'fetch_ms'],
    ['sql.stats_spool', 'spool_ms'],
    ['sql.stats_total', 'total_ms'],
  ] as const) {
    rows.push({
      label: t(labelKey),
      value: formatTiming(rt.timings?.[timingKey]),
    })
  }
  rows.push({
    label: t('sql.stats_total_rows'),
    value: res?.total_rows != null ? String(res.total_rows) : '-',
  })
  rows.push({ label: t('sql.stats_columns'), value: res ? String(res.columns.length) : '-' })
  rows.push({
    label: t('sql.stats_truncated'),
    value: res?.truncated ? t('sql.stats_yes') : t('sql.stats_no'),
  })
  const execution = rt.progress?.execution
  rows.push({
    label: t('sql.stats_rows_read'),
    value: execution?.rows_read != null ? String(execution.rows_read) : '-',
  })
  rows.push({
    label: t('sql.stats_rows_returned'),
    value: execution?.rows_returned != null ? String(execution.rows_returned) : '-',
  })
  rows.push({ label: t('sql.stats_query_shape'), value: execution?.query_shape ?? '-' })
  rows.push({
    label: t('sql.stats_limit_pushdown'),
    value:
      execution?.limit_pushdown == null
        ? '-'
        : execution.limit_pushdown
          ? t('sql.stats_yes')
          : t('sql.stats_output_only'),
  })
  if (rt.jobId) rows.push({ label: t('sql.stats_job_id'), value: rt.jobId })
  return rows
})

watch(datasources, (list) => {
  if (!selectedDsId.value && list.length > 0) selectedDsId.value = list[0].id
})

watch(activeConsoleId, (current, previous) => {
  if (previous) stopConsolePoll(previous)
  const consoleRow = activeConsole.value
  suppressConsoleSave.value = true
  editorSql.value = consoleRow?.sql ?? 'SELECT 1 AS hello;'
  const datasourceId = consoleRow?.datasource_id ?? datasources.value[0]?.id ?? ''
  selectedDsId.value = datasourceId
  // The displayed default is a real console binding, not a visual fallback.
  // This assignment happens while the generic watcher is suppressed, so persist
  // it explicitly before lazy attach reads the console row from the API.
  if (consoleRow && !consoleRow.datasource_id && datasourceId) {
    patchLocalConsole(consoleRow.id, { datasource_id: datasourceId })
    scheduleConsolePatch(consoleRow.id, { datasource_id: datasourceId })
  }
  void nextTick(() => {
    suppressConsoleSave.value = false
    if (current) resumeConsolePoll(current)
  })
})

watch(editorSql, (value) => {
  if (suppressConsoleSave.value || !activeConsole.value || editorReadOnly.value) return
  patchLocalConsole(activeConsole.value.id, { sql: value })
  scheduleConsolePatch(activeConsole.value.id, { sql: value })
})

// 切 console → 清体检卡(findings 锚的是上一个 console 的 SQL 文本)。
watch(activeConsoleId, () => {
  preflightFindings.value = null
})

// 切 console → 会话只做**单次 observe**(设计 §3.3):空闲期不轮询,
// 也不因为切过去看一眼就把会话建起来。没绑过会话的 console 什么都不发。
watch(activeConsoleId, (current) => {
  if (current) void consoleSessions.observe(current)
})

watch(selectedDsId, (value, previous) => {
  if (value !== previous) aiPanelOpen.value = false
  execError.value = null
  if (suppressConsoleSave.value || !activeConsole.value || editorReadOnly.value) return
  const datasourceId = value || null
  patchLocalConsole(activeConsole.value.id, { datasource_id: datasourceId })
  scheduleConsolePatch(activeConsole.value.id, { datasource_id: datasourceId })
})

watch(sidebarTab, (tab) => {
  if (tab === 'history') void loadHistory()
  if (tab === 'templates') void loadTemplates()
  if (tab === 'metadata') {
    if (!metadataDsId.value) metadataDsId.value = selectedDsId.value || datasources.value[0]?.id || ''
    if (metadataDsId.value && metadataSchemas.value.length === 0) void loadMetadataSchemas(false)
  }
})
watch([historyDatasourceId, historyRange], () => {
  if (sidebarTab.value === 'history') void loadHistory()
})
watch(templateSearch, () => {
  if (sidebarTab.value === 'templates') void loadTemplates()
})
watch(metadataDsId, () => {
  metadataSchemas.value = []
  metadataError.value = null
  if (sidebarTab.value === 'metadata' && metadataDsId.value) void loadMetadataSchemas(false)
})

onMounted(async () => {
  if (typeof BroadcastChannel !== 'undefined') {
    progressChannel = new BroadcastChannel('dataops-sql-job-progress')
    progressChannel.addEventListener('message', onProgressBroadcast)
  }
  document.addEventListener('visibilitychange', onVisibilityChange)
  nowTimer = setInterval(() => {
    nowMs.value = Date.now()
  }, 500)
  await loadConsoles()
})

onUnmounted(() => {
  void cancelConsoleQueries()
  sqlEditor = null
  for (const timer of pollTimers.values()) clearTimeout(timer)
  for (const controller of pollControllers.values()) controller.abort()
  for (const runtimeList of Object.values(runtimes)) {
    for (const runtime of runtimeList) {
      cancelResultRead(runtime)
      const key = runtimeKey(runtime)
      if (key) releasePollLease(key)
    }
  }
  progressChannel?.removeEventListener('message', onProgressBroadcast)
  progressChannel?.close()
  progressChannel = null
  document.removeEventListener('visibilitychange', onVisibilityChange)
  for (const timer of saveTimers.values()) clearTimeout(timer)
  for (const timer of exportPollTimers) clearTimeout(timer)
  if (nowTimer !== null) clearInterval(nowTimer)
})

function createRuntime(statement = '', statementIndex = 0, statementCount = 1): ConsoleRuntime {
  return {
    statement,
    statementIndex,
    statementCount,
    jobId: null,
    rootJobId: null,
    statementId: null,
    clientRequestId: null,
    statementEpoch: 0,
    resultSetId: null,
    status: null,
    error: null,
    message: null,
    cancelling: false,
    result: null,
    resultLoading: false,
    pageOffset: 0,
    pageSize: pageSizeSelection.value,
    startedAt: null,
    finishedAt: null,
    timings: null,
    progress: null,
    resultVersion: 0,
    resultGeneration: 0,
    unchangedPolls: 0,
    networkFailures: 0,
    planJobId: null,
    planStatus: null,
    planResult: null,
    planError: null,
    resultTab: 'result',
  }
}

function runtimesFor(consoleId: string): ConsoleRuntime[] {
  if (!runtimes[consoleId]) runtimes[consoleId] = [createRuntime()]
  return runtimes[consoleId]
}

function runtimeFor(consoleId: string): ConsoleRuntime {
  const items = runtimesFor(consoleId)
  return items[activeRuntimeIndexes[consoleId] ?? 0] ?? items[0]
}

async function loadConsoles(): Promise<void> {
  consolesLoading.value = true
  consoleError.value = null
  try {
    consoles.value = await listSqlConsoles()
    if (!activeConsoleId.value || !consoles.value.some((item) => item.id === activeConsoleId.value)) {
      activeConsoleId.value = sortedConsoles.value[0]?.id ?? null
    }
  } catch (e) {
    consoleError.value = errorMessage(e)
  } finally {
    consolesLoading.value = false
  }
}

async function createConsole(): Promise<void> {
  if (writesBlocked.value) return
  consoleError.value = null
  try {
    await flushAllConsolePatches()
    const created = await createSqlConsole({
      name: nextConsoleName(),
      datasource_id: selectedDsId.value || datasources.value[0]?.id || null,
      sql: 'SELECT 1 AS hello;',
    })
    consoles.value = [created, ...consoles.value]
    activeConsoleId.value = created.id
    sidebarTab.value = 'consoles'
  } catch (e) {
    consoleError.value = errorMessage(e)
  }
}

async function deleteConsole(consoleId: string): Promise<void> {
  if (writesBlocked.value) return
  consoleError.value = null
  try {
    await cancelConsoleQueries(consoleId)
    // console 没了,会话的绑定锚点也就没了:先关会话再删,避免留一条
    // 只能等空闲回收的连接。关不掉不阻塞删除 —— 空闲 Timer 会兜底。
    await consoleSessions.close(consoleId).catch(() => {})
    consoleSessions.forget(consoleId)
    await deleteSqlConsole(consoleId)
    consoles.value = consoles.value.filter((item) => item.id !== consoleId)
    stopConsolePoll(consoleId)
    delete runtimes[consoleId]
    delete activeRuntimeIndexes[consoleId]
    if (activeConsoleId.value === consoleId) {
      activeConsoleId.value = sortedConsoles.value[0]?.id ?? null
    }
  } catch (e) {
    consoleError.value = errorMessage(e)
  }
}

async function togglePinned(consoleRow: SqlConsole): Promise<void> {
  if (writesBlocked.value) return
  await patchConsole(consoleRow.id, { pinned: !consoleRow.pinned })
}

function startRename(consoleRow: SqlConsole): void {
  if (writesBlocked.value) return
  renameConsoleId.value = consoleRow.id
  renameConsoleDraft.value = consoleRow.name
}

async function submitRename(): Promise<void> {
  if (writesBlocked.value) return
  if (!renameConsoleId.value || !renameConsoleDraft.value.trim()) return
  await patchConsole(renameConsoleId.value, { name: renameConsoleDraft.value.trim() })
  renameConsoleId.value = null
  renameConsoleDraft.value = ''
}

async function patchConsole(consoleId: string, patch: SqlConsoleUpdateRequest): Promise<void> {
  if (writesBlocked.value) return
  patchLocalConsole(consoleId, patch)
  try {
    const updated = await updateSqlConsole(consoleId, patch)
    mergeConsole(updated)
  } catch (e) {
    consoleError.value = errorMessage(e)
    await loadConsoles()
  }
}

function scheduleConsolePatch(consoleId: string, patch: SqlConsoleUpdateRequest): void {
  if (writesBlocked.value) return
  pendingConsolePatches.set(consoleId, {
    ...(pendingConsolePatches.get(consoleId) ?? {}),
    ...patch,
  })
  const oldTimer = saveTimers.get(consoleId)
  if (oldTimer) clearTimeout(oldTimer)
  saveTimers.set(
    consoleId,
    setTimeout(() => {
      void flushConsolePatch(consoleId)
    }, SAVE_DEBOUNCE_MS),
  )
}

async function flushAllConsolePatches(): Promise<void> {
  await Promise.all([...pendingConsolePatches.keys()].map((consoleId) => flushConsolePatch(consoleId)))
}

async function flushConsolePatch(consoleId: string): Promise<void> {
  const timer = saveTimers.get(consoleId)
  if (timer) clearTimeout(timer)
  saveTimers.delete(consoleId)
  const patch = pendingConsolePatches.get(consoleId)
  if (!patch) return
  pendingConsolePatches.delete(consoleId)
  if (writesBlocked.value) return
  try {
    const updated = await updateSqlConsole(consoleId, patch)
    mergeConsole(updated)
  } catch (e) {
    consoleError.value = errorMessage(e)
  }
}

function patchLocalConsole(consoleId: string, patch: SqlConsoleUpdateRequest): void {
  consoles.value = consoles.value.map((item) =>
    item.id === consoleId
      ? {
          ...item,
          ...patch,
          updated_at: new Date().toISOString(),
        }
      : item,
  )
}

function mergeConsole(consoleRow: SqlConsole): void {
  const pending = pendingConsolePatches.get(consoleRow.id)
  const reconciled = pending ? { ...consoleRow, ...pending } : consoleRow
  const index = consoles.value.findIndex((item) => item.id === consoleRow.id)
  if (index === -1) consoles.value = [reconciled, ...consoles.value]
  else consoles.value.splice(index, 1, reconciled)
}

function nextConsoleName(): string {
  return `query_${consoles.value.length + 1}.sql`
}

interface EditorSqlTarget {
  sql: string
  start: number
  end: number
}

function selectedEditorSql(): string {
  const selection = sqlEditor?.getSelection()
  const model = sqlEditor?.getModel()
  if (!selection || !model || selection.isEmpty()) return ''
  return model.getValueInRange(selection)
}

function editorSqlTarget(): EditorSqlTarget | null {
  const selection = sqlEditor?.getSelection()
  const model = sqlEditor?.getModel()
  if (selection && model && !selection.isEmpty()) {
    return {
      sql: model.getValueInRange(selection),
      start: model.getOffsetAt(selection.getStartPosition()),
      end: model.getOffsetAt(selection.getEndPosition()),
    }
  }

  const statements = splitSqlStatements(editorSql.value)
  if (statements.length === 0) return null
  if (!model || statements.length === 1) return statements[0]
  const position = sqlEditor?.getPosition()
  const offset = position ? model.getOffsetAt(position) : 0
  return statementAtOffset(editorSql.value, offset)
}

function replaceEditorSqlTarget(target: EditorSqlTarget, replacement: string): void {
  editorSql.value = `${editorSql.value.slice(0, target.start)}${replacement}${editorSql.value.slice(target.end)}`
}

function selectRuntime(index: number): void {
  if (!activeConsoleId.value || !activeRuntimes.value[index]) return
  activeRuntimeIndexes[activeConsoleId.value] = index
  resetExportState()
}

async function onExecute(): Promise<void> {
  if (writesBlocked.value) {
    execError.value = t('license.writes_blocked')
    return
  }
  const consoleRow = activeConsole.value
  const requestedMaxRows = queryMaxRows.value
  if (!consoleRow || !selectedDsId.value || !editorSql.value.trim()) {
    execError.value = t('sql.error_pick_ds_or_sql')
    return
  }
  if (unsupportedDb.value) {
    execError.value = t('sql.unsupported_db_error', { db: unsupportedDb.value })
    return
  }
  if (requestedMaxRows === null) {
    execError.value = t('sql.max_rows_invalid', { max: QUERY_MAX_ROWS_LIMIT })
    return
  }
  execError.value = null
  await flushConsolePatch(consoleRow.id)
  const selectedSql = selectedEditorSql()
  const statements = splitSqlStatements(selectedSql || editorSql.value)
  if (statements.length === 0) {
    execError.value = t('sql.error_pick_ds_or_sql')
    return
  }

  await cancelConsoleQueries(consoleRow.id)
  stopConsolePoll(consoleRow.id)
  const nextRuntimes = statements.map((statement, index) => {
    const runtime = createRuntime(statement.sql, index, statements.length)
    runtime.status = 'pending'
    return runtime
  })
  runtimes[consoleRow.id] = nextRuntimes
  activeRuntimeIndexes[consoleRow.id] = 0
  // 从 reactive 容器重新取 proxy；后续异步轮询必须修改 proxy，才能即时刷新结果区。
  const batchRuntimes = runtimesFor(consoleRow.id)
  scrollResultIntoView()

  // 懒 attach:走到这里才建会话。返回 null = 该部署/方言没有会话,回落 job 路径。
  let session: ConsoleSessionState | null = null
  try {
    session = await consoleSessions.ensureSession(consoleRow.id, selectedDs.value?.db_type ?? null)
  } catch (e) {
    // attach 本身失败(建连失败 / 会话数护栏)——不静默降级成 job 路径,
    // 否则用户以为拿到的是会话语义,实际不是。
    failBatch(batchRuntimes, errorMessage(e))
    return
  }
  if (session) {
    await submitSessionBatch(consoleRow.id, session, batchRuntimes, requestedMaxRows)
    return
  }

  await Promise.all(
    batchRuntimes.map(async (runtime, index) => {
      try {
        const response = await executeSql({
          datasource_id: selectedDsId.value,
          // A console owns one latest result set. Earlier batch results stay independent
          // so the server's existing console-result eviction cannot remove sibling results.
          console_id: index === batchRuntimes.length - 1 ? consoleRow.id : null,
          sql: runtime.statement,
          page_size: runtime.pageSize,
          max_result_rows: requestedMaxRows,
        })
        runtime.jobId = response.job_id
        runtime.rootJobId = response.job_id
        runtime.resultSetId = response.result_set_id
        startConsolePoll(consoleRow.id, runtime)
      } catch (e) {
        runtime.status = null
        runtime.error = errorMessage(e)
        runtime.finishedAt = Date.now()
      }
    }),
  )
}

function failBatch(batchRuntimes: ConsoleRuntime[], message: string): void {
  for (const runtime of batchRuntimes) {
    runtime.status = null
    runtime.error = message
    runtime.finishedAt = Date.now()
  }
}

/** 未提交出去的语句标成 cancelled + 「已跳过」——不留一排假的 pending。 */
function skipRemaining(batchRuntimes: ConsoleRuntime[], fromIndex: number): void {
  for (const runtime of batchRuntimes.slice(fromIndex)) {
    runtime.status = 'cancelled'
    runtime.message = t('sql.session_statement_skipped')
    runtime.finishedAt = Date.now()
  }
}

/**
 * 同 console **按序提交**(设计 §3.3 行为变化)。
 *
 * 旧行为是 `Promise.all` 并行提交每条语句各起一个 job;会话模型下一条连接一个
 * lane,同 console 语句天然串行(DataGrip 语义,也是会话连续性的前提)。
 * 这里只保证**提交顺序**——lane 按 seq 顺序执行,不必等前一条终态再提交下一条,
 * 于是 statement tab 条能立刻列全,逐条状态由各自轮询更新。
 *
 * 每条语句一个独立 `client_request_id`:提交响应丢了可安全重发提交请求,
 * 服务端按 (session_id, client_request_id) 去重,永不重执行。
 */
async function submitSessionBatch(
  consoleId: string,
  session: ConsoleSessionState,
  batchRuntimes: ConsoleRuntime[],
  requestedMaxRows: number,
): Promise<void> {
  const sessionId = session.sessionId
  if (!sessionId) return
  for (const [index, runtime] of batchRuntimes.entries()) {
    runtime.clientRequestId = newClientRequestId()
    runtime.statementEpoch = session.epoch
    try {
      const response = await submitStatement(sessionId, {
        epoch: session.epoch,
        sql: runtime.statement,
        client_request_id: runtime.clientRequestId,
        page_size: runtime.pageSize,
        max_result_rows: requestedMaxRows,
      })
      runtime.statementId = response.statement_id
      runtime.resultSetId = response.result_set_id
      startConsolePoll(consoleId, runtime)
    } catch (e) {
      handleSessionConflict(consoleId, e)
      runtime.status = null
      runtime.error = errorMessage(e)
      runtime.finishedAt = Date.now()
      // 前一条已经排进 lane 了,后面的不再提交:批次语义是「按序」,
      // 中间断了就断了,不能跳过一条继续送后面的。
      skipRemaining(batchRuntimes, index + 1)
      return
    }
  }
}

function newClientRequestId(): string {
  return globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`
}

/**
 * 会话冲突 409 → 会话态。`stale_session_epoch` 随体带 current_epoch(M1),
 * 直接渲染接管提示;`session_lost` / `session_not_active` 落 session_lost banner。
 */
function handleSessionConflict(consoleId: string, error: unknown): void {
  if (!(error instanceof ApiError) || error.status !== 409) return
  const code = error.code ?? ''
  if (code === 'stale_session_epoch') {
    consoleSessions.applyStaleEpoch(consoleId, error.body?.current_epoch)
    return
  }
  if (code === 'session_lost' || code === 'session_not_active') {
    consoleSessions.markLost(consoleId, code)
  }
}

async function onCancel(): Promise<void> {
  if (writesBlocked.value) return
  const consoleId = activeConsoleId.value
  const cancellable = activeRuntimes.value.filter(
    (runtime) =>
      runtimeKey(runtime) && runtime.status && ACTIVE.has(runtime.status) && !runtime.cancelling,
  )
  await Promise.all(
    cancellable.map(async (runtime) => {
      runtime.cancelling = true
      try {
        await cancelRuntime(runtime)
      } catch (e) {
        runtime.cancelling = false
        if (consoleId) handleSessionConflict(consoleId, e)
        runtime.error = errorMessage(e)
      }
    }),
  )
}

/**
 * 取消一条 runtime。会话路径带上提交时的 epoch —— 取消权随 epoch 移交(M8),
 * 旧 tab 的 cancel 得 409,不允许「看不见结果的窗口杀掉别人正观察的查询」。
 *
 * 会话取消**不是**协作式软取消:排队中出队,执行中走控制连接硬取消阶梯
 * (设计 §4.1)。`server_cancel=degraded` 的数据源会退化为断连兜底,
 * 这一点由状态条如实告知,前端不在这里替用户做判断。
 */
function cancelRuntime(runtime: ConsoleRuntime): Promise<unknown> {
  if (runtime.statementId) {
    return cancelStatement(runtime.statementId, runtime.statementEpoch).then((receipt) => {
      // 服务端说没受理(已终态)就别把按钮永远停在「取消中」。
      if (!receipt.accepted) runtime.cancelling = false
      return receipt
    })
  }
  return cancelJob(runtime.jobId as string)
}

// ── 会话生命周期(懒 attach / 重连 / 关闭)─────────────────────────
/** 「重新连接」:接管回持有权或重建丢失的会话,并清掉旧语句身份。 */
async function onReconnectSession(): Promise<void> {
  const consoleId = activeConsoleId.value
  if (!consoleId || sessionBusy.value) return
  sessionBusy.value = true
  execError.value = null
  try {
    stopConsolePoll(consoleId)
    for (const runtime of runtimes[consoleId] ?? []) {
      // 旧会话的语句在新会话里没有身份,继续轮询只会一直 404。
      if (runtime.status && !TERMINAL.has(runtime.status)) runtime.status = 'cancelled'
      runtime.statementId = null
    }
    await consoleSessions.reattach(consoleId, selectedDs.value?.db_type ?? null)
  } catch (e) {
    execError.value = errorMessage(e)
  } finally {
    sessionBusy.value = false
  }
}

/** 显式关闭会话(先取消在跑语句 → rollback → 断连,服务端做)。 */
async function onCloseSession(): Promise<void> {
  const consoleId = activeConsoleId.value
  if (!consoleId || sessionBusy.value) return
  sessionBusy.value = true
  try {
    stopConsolePoll(consoleId)
    await consoleSessions.close(consoleId)
  } catch (e) {
    execError.value = errorMessage(e)
  } finally {
    sessionBusy.value = false
  }
}

/**
 * 轮询机器的身份键。job 路径是 job_id,会话路径是 statement_id —— 两者都只被
 * 当作「这一格结果属于谁」的标识用(定时器 key / 跨 tab lease / 广播匹配),
 * 于是整套自适应退避、lease、渐进读取一行不改地服务两条路径(设计 §3.2)。
 */
function runtimeKey(runtime: ConsoleRuntime): string | null {
  return runtime.statementId ?? runtime.jobId
}

function startConsolePoll(consoleId: string, runtime: ConsoleRuntime): void {
  scheduleConsolePoll(
    consoleId,
    runtime,
    document.hidden ? QUERY_POLL_HIDDEN_MS : POLL_MS,
  )
}

function stopConsolePoll(consoleId: string): void {
  const prefix = `query:${consoleId}:`
  for (const [key, timer] of pollTimers.entries()) {
    if (!key.startsWith(prefix)) continue
    clearTimeout(timer)
    pollTimers.delete(key)
  }
  for (const [key, controller] of pollControllers.entries()) {
    if (!key.startsWith(prefix)) continue
    controller.abort()
    pollControllers.delete(key)
  }
  for (const runtime of runtimes[consoleId] ?? []) {
    cancelResultRead(runtime)
    const key = runtimeKey(runtime)
    if (key) releasePollLease(key)
  }
}

function resumeConsolePoll(consoleId: string): void {
  for (const runtime of runtimes[consoleId] ?? []) {
    if (runtimeKey(runtime) && runtime.status && ACTIVE.has(runtime.status)) {
      startConsolePoll(consoleId, runtime)
    }
  }
}

async function cancelConsoleQueries(consoleId?: string): Promise<void> {
  if (writesBlocked.value) return
  const consoleIds = consoleId ? [consoleId] : Object.keys(runtimes)
  const pending = consoleIds.flatMap((id) =>
    (runtimes[id] ?? []).filter(
      (runtime) => runtimeKey(runtime) && runtime.status && ACTIVE.has(runtime.status),
    ),
  )
  if (pending.length === 0) return
  await Promise.allSettled(pending.map((runtime) => cancelRuntime(runtime)))
}

async function pollConsole(consoleId: string, runtime: ConsoleRuntime): Promise<void> {
  const key = runtimeKey(runtime)
  if (!key) return
  if (!runtimes[consoleId]?.includes(runtime)) return
  if (activeConsoleId.value !== consoleId) return
  const pollKey = `query:${consoleId}:${key}`
  pollTimers.delete(pollKey)
  if (!tryAcquirePollLease(key)) {
    scheduleConsolePoll(consoleId, runtime, POLL_MS)
    return
  }
  const controller = new AbortController()
  pollControllers.get(pollKey)?.abort()
  pollControllers.set(pollKey, controller)
  const resultGeneration = runtime.resultGeneration
  let nextDelay = POLL_MS
  try {
    const progress = await loadRuntimeProgress(consoleId, runtime, controller.signal)
    if (runtimeKey(runtime) !== key || !runtimes[consoleId]?.includes(runtime)) return
    await consumeJobProgress(runtime, progress, controller.signal, resultGeneration)
    progressChannel?.postMessage({ source: pollingTabId, progress })
    if (progress.terminal) {
      releasePollLease(key)
      return
    }
    nextDelay = nextQueryPollDelay(runtime, progress.retry_after_ms)
  } catch (e) {
    if (isAbortError(e)) return
    runtime.networkFailures += 1
    if (e instanceof ApiError && (e.status === 429 || e.status === 0)) {
      nextDelay = nextQueryPollErrorDelay(runtime, e.retryAfterMs)
    } else {
      runtime.error = errorMessage(e)
      nextDelay = nextQueryPollErrorDelay(runtime)
    }
  } finally {
    if (pollControllers.get(pollKey) === controller) pollControllers.delete(pollKey)
  }
  if (runtimeKey(runtime) === key && runtime.status && !TERMINAL.has(runtime.status)) {
    scheduleConsolePoll(consoleId, runtime, nextDelay)
  }
}

function scheduleConsolePoll(consoleId: string, runtime: ConsoleRuntime, delayMs: number): void {
  const key = runtimeKey(runtime)
  if (!key || activeConsoleId.value !== consoleId) return
  const pollKey = `query:${consoleId}:${key}`
  const existing = pollTimers.get(pollKey)
  if (existing) clearTimeout(existing)
  pollTimers.set(
    pollKey,
    setTimeout(() => {
      void pollConsole(consoleId, runtime)
    }, Math.max(POLL_MS, document.hidden ? QUERY_POLL_HIDDEN_MS : delayMs)),
  )
}

/**
 * 一次轮询 = 一条 progress。会话路径读语句 progress 后转成 job progress 形状,
 * 并把内嵌的 `session` 块喂给会话状态条 —— 执行期间只有这一条轮询循环,
 * 语句与会话状态同时刷新(设计 §3.2),空闲期不轮询。
 */
async function loadRuntimeProgress(
  consoleId: string,
  runtime: ConsoleRuntime,
  signal: AbortSignal,
): Promise<JobProgressResponse> {
  const statementId = runtime.statementId
  if (statementId) {
    const progress = await getStatementProgress(statementId, runtime.resultVersion, signal)
    consoleSessions.applySessionBlock(consoleId, progress.session)
    return toJobProgress(progress)
  }
  return loadJobProgress(runtime.jobId as string, runtime, signal)
}

async function loadJobProgress(
  jobId: string,
  runtime: ConsoleRuntime,
  signal: AbortSignal,
): Promise<JobProgressResponse> {
  try {
    return await getJobProgress(jobId, runtime.resultVersion, signal)
  } catch (e) {
    if (!(e instanceof ApiError) || e.status !== 404) throw e
    // Rolling-upgrade fallback for an older API. It intentionally stays on the
    // adaptive loop and is removed once every deployment exposes /progress.
    const job = await getJob(jobId)
    return legacyJobProgress(job, runtime.resultVersion)
  }
}

function legacyJobProgress(job: JobResponse, resultVersion: number): JobProgressResponse {
  const terminal = TERMINAL.has(job.status)
  return {
    job_id: job.id,
    result_set_id: job.result_set_id,
    status: job.status,
    loaded_rows: 0,
    result_version: resultVersion + (job.status === 'pending' ? 0 : 1),
    columns_ready: job.status !== 'pending',
    first_batch_ready: job.status !== 'pending',
    terminal,
    error: job.error,
    error_code: job.error_code,
    retry_after_ms: terminal ? 0 : POLL_MS,
    has_new_result: job.status !== 'pending',
    truncated: null,
    has_more: null,
    timings: job.timings ?? null,
    execution: null,
  }
}

async function consumeJobProgress(
  runtime: ConsoleRuntime,
  progress: JobProgressResponse,
  signal?: AbortSignal,
  expectedResultGeneration = runtime.resultGeneration,
): Promise<{
  accepted: boolean
  resultGeneration: number
}> {
  if (signal?.aborted || runtime.resultGeneration !== expectedResultGeneration) {
    return { accepted: false, resultGeneration: runtime.resultGeneration }
  }
  applyJobProgress(runtime, progress)
  let readOutcome: ResultReadOutcome | null = null
  const hasUnconsumedResult = progress.result_version > runtime.resultVersion
  if (hasUnconsumedResult && (progress.columns_ready || progress.first_batch_ready)) {
    const currentPageComplete =
      runtime.result?.offset === runtime.pageOffset && runtime.result.rows.length >= runtime.pageSize
    if (currentPageComplete) {
      // ResultStore only appends rows, so later versions cannot change an already full page.
      runtime.resultVersion = progress.result_version
    } else {
      readOutcome = runtime.result && runtime.result.offset === runtime.pageOffset
        ? await appendResultDelta(runtime, signal)
        : await fetchResult(runtime, runtime.pageOffset, false, signal)
      if (readOutcome.applied) runtime.resultVersion = progress.result_version
    }
  } else if (progress.result_version === 0 && progress.terminal && progress.columns_ready) {
    readOutcome = await fetchResult(runtime, runtime.pageOffset, false, signal)
  }
  if (readOutcome && runtime.resultGeneration !== readOutcome.generation) {
    return { accepted: false, resultGeneration: runtime.resultGeneration }
  }
  // A terminal consume may have awaited a result read that advanced the
  // generation. In that case the read's own finally owns loading cleanup;
  // never clear a newer page request started while the old read was pending.
  if (progress.terminal && runtime.resultGeneration === expectedResultGeneration) {
    runtime.resultLoading = false
  }
  if (hasUnconsumedResult) {
    runtime.unchangedPolls = 0
  } else {
    runtime.unchangedPolls += 1
  }
  runtime.networkFailures = 0
  return { accepted: true, resultGeneration: runtime.resultGeneration }
}

function applyJobProgress(runtime: ConsoleRuntime, progress: JobProgressResponse): void {
  const enteringTerminal = progress.terminal && !TERMINAL.has(runtime.status ?? 'pending')
  const execution = progress.execution
  if (ACTIVE.has(progress.status) && runtime.startedAt === null) {
    runtime.startedAt = parseTimeMs(execution?.claimed_at) ?? parseTimeMs(execution?.queued_at)
  }
  if (enteringTerminal) {
    runtime.startedAt =
      parseTimeMs(execution?.claimed_at) ?? parseTimeMs(execution?.queued_at) ?? runtime.startedAt
    runtime.finishedAt = parseTimeMs(execution?.finished_at)
  }
  runtime.status = progress.status
  runtime.resultSetId = progress.result_set_id
  runtime.error = progress.error
  runtime.message = progress.error
  runtime.timings = progress.timings ?? runtime.timings
  runtime.progress = progress
  if (runtime.result) {
    runtime.result.loaded_rows = progress.loaded_rows
    runtime.result.truncated = progress.truncated
    runtime.result.has_more = progress.has_more
    runtime.result.pagination_mode = progress.pagination_mode
    runtime.result.pagination_reason = progress.pagination_reason
  }
  if (progress.terminal) runtime.cancelling = false
}

/** 结果页读取:会话语句读 spool 与 job 同形(设计 §3.1),只差端点。 */
function readResultPage(
  runtime: ConsoleRuntime,
  offset: number,
  limit: number,
  signal?: AbortSignal,
): Promise<JobResultResponse> {
  const statementId = runtime.statementId
  if (statementId) {
    return getStatementResult(statementId, offset, limit, signal).then(toJobResult)
  }
  return getJobResult(runtime.jobId as string, offset, limit, signal)
}

async function fetchResult(
  runtime: ConsoleRuntime,
  offset: number,
  showLoading: boolean = true,
  signal?: AbortSignal,
): Promise<ResultReadOutcome> {
  const key = runtimeKey(runtime)
  if (!key) return { applied: false, generation: runtime.resultGeneration }
  const generation = advanceResultGeneration(runtime)
  if (showLoading) runtime.resultLoading = true
  try {
    const result = await readResultPage(runtime, offset, runtime.pageSize, signal)
    if (signal?.aborted || runtimeKey(runtime) !== key || runtime.resultGeneration !== generation) {
      return { applied: false, generation }
    }
    runtime.result = result
    runtime.pageOffset = offset
    return { applied: true, generation }
  } catch (e) {
    if (isAbortError(e)) throw e
    if (runtimeKey(runtime) !== key || runtime.resultGeneration !== generation) {
      return { applied: false, generation }
    }
    if (isResultNotReady(e, runtime)) return { applied: false, generation }
    runtime.error = errorMessage(e)
    return { applied: false, generation }
  } finally {
    if (runtime.resultGeneration === generation) runtime.resultLoading = false
  }
}

/**
 * Read only the missing tail of the visible page. ResultStore pages are
 * append-only, so replacing an incomplete page would needlessly discard DOM
 * rows and re-render every cell. The generation check makes an aborted or
 * superseded request unable to contaminate the current page.
 */
async function appendResultDelta(runtime: ConsoleRuntime, signal?: AbortSignal): Promise<ResultReadOutcome> {
  const key = runtimeKey(runtime)
  if (!key || !runtime.result) {
    return { applied: false, generation: runtime.resultGeneration }
  }
  const current = runtime.result
  if (current.offset !== runtime.pageOffset || current.rows.length >= runtime.pageSize) {
    return { applied: true, generation: runtime.resultGeneration }
  }
  const offset = current.offset + current.rows.length
  const limit = runtime.pageSize - current.rows.length
  if (limit <= 0) return { applied: true, generation: runtime.resultGeneration }
  const generation = advanceResultGeneration(runtime)
  try {
    const delta = await readResultPage(runtime, offset, limit, signal)
    if (
      signal?.aborted ||
      runtimeKey(runtime) !== key ||
      runtime.resultGeneration !== generation ||
      runtime.result !== current ||
      delta.offset !== offset
    ) {
      return { applied: false, generation }
    }
    const appendedRows = delta.rows.slice(0, limit)
    runtime.result = {
      ...current,
      columns: current.columns.length > 0 ? current.columns : delta.columns,
      rows: [...current.rows, ...appendedRows],
      loaded_rows: delta.loaded_rows ?? current.loaded_rows,
      total_rows: delta.total_rows ?? current.total_rows,
      state: delta.state ?? current.state,
      truncated: delta.truncated ?? current.truncated,
      has_more: delta.has_more ?? current.has_more,
      page_size: delta.page_size ?? current.page_size,
      max_result_rows: delta.max_result_rows ?? current.max_result_rows,
      // The API counts omitted previews in the returned range. Once ranges
      // are merged into one visible page, retain the count for both ranges.
      preview_truncated_cells:
        (current.preview_truncated_cells ?? 0) + (delta.preview_truncated_cells ?? 0),
      pagination_mode: delta.pagination_mode ?? current.pagination_mode,
      pagination_reason: delta.pagination_reason ?? current.pagination_reason,
    }
    return { applied: true, generation }
  } catch (e) {
    if (isAbortError(e)) throw e
    if (runtimeKey(runtime) !== key || runtime.resultGeneration !== generation) {
      return { applied: false, generation }
    }
    if (isResultNotReady(e, runtime)) return { applied: false, generation }
    runtime.error = errorMessage(e)
    return { applied: false, generation }
  }
}

function nextQueryPollDelay(runtime: ConsoleRuntime, serverRetryMs: number): number {
  const index = Math.min(runtime.unchangedPolls, QUERY_POLL_BACKOFF_MS.length - 1)
  const adaptive = QUERY_POLL_BACKOFF_MS[index]
  return Math.max(serverRetryMs, document.hidden ? QUERY_POLL_HIDDEN_MS : adaptive)
}

function isResultNotReady(error: unknown, runtime: ConsoleRuntime): boolean {
  return (
    error instanceof ApiError &&
    error.status === 409 &&
    runtime.status !== null &&
    !TERMINAL.has(runtime.status)
  )
}

function advanceResultGeneration(runtime: ConsoleRuntime): number {
  runtime.resultGeneration += 1
  return runtime.resultGeneration
}

function cancelResultRead(runtime: ConsoleRuntime): number {
  const generation = advanceResultGeneration(runtime)
  runtime.resultLoading = false
  return generation
}

function isCurrentRuntime(consoleId: string, runtime: ConsoleRuntime): boolean {
  return activeConsoleId.value === consoleId && runtimes[consoleId]?.includes(runtime) === true
}

function nextQueryPollErrorDelay(runtime: ConsoleRuntime, retryAfterMs?: number): number {
  const exponential = Math.min(5000, 1000 * 2 ** Math.min(runtime.networkFailures - 1, 3))
  return Math.max(retryAfterMs ?? 0, document.hidden ? QUERY_POLL_HIDDEN_MS : exponential)
}

function isAbortError(error: unknown): boolean {
  return error instanceof Error && error.name === 'AbortError'
}

function tryAcquirePollLease(runtimeId: string): boolean {
  const now = Date.now()
  const key = `${QUERY_POLL_LEASE_PREFIX}${runtimeId}`
  try {
    const current = JSON.parse(localStorage.getItem(key) ?? 'null') as {
      owner?: string
      expiresAt?: number
    } | null
    if (current?.owner !== pollingTabId && (current?.expiresAt ?? 0) > now) return false
    localStorage.setItem(
      key,
      JSON.stringify({ owner: pollingTabId, expiresAt: now + QUERY_POLL_LEASE_MS }),
    )
    const confirmed = JSON.parse(localStorage.getItem(key) ?? 'null') as { owner?: string } | null
    return confirmed?.owner === pollingTabId
  } catch {
    return true
  }
}

function releasePollLease(runtimeId: string): void {
  const key = `${QUERY_POLL_LEASE_PREFIX}${runtimeId}`
  try {
    const current = JSON.parse(localStorage.getItem(key) ?? 'null') as { owner?: string } | null
    if (current?.owner === pollingTabId) localStorage.removeItem(key)
  } catch {
    // Storage may be unavailable in privacy modes; the per-page loop remains safe.
  }
}

function onProgressBroadcast(event: MessageEvent<unknown>): void {
  const message = event.data as { source?: string; progress?: JobProgressResponse } | null
  if (!message?.progress || message.source === pollingTabId) return
  const progress = message.progress
  for (const [consoleId, runtimeList] of Object.entries(runtimes)) {
    for (const runtime of runtimeList) {
      if (runtimeKey(runtime) !== progress.job_id) continue
      if (activeConsoleId.value !== consoleId) {
        applyJobProgress(runtime, progress)
        continue
      }
      const broadcastKey = runtimeKey(runtime)
      void consumeJobProgress(runtime, progress, undefined, runtime.resultGeneration).then((outcome) => {
        if (
          !outcome.accepted ||
          !isCurrentRuntime(consoleId, runtime) ||
          runtimeKey(runtime) !== broadcastKey ||
          runtime.resultGeneration !== outcome.resultGeneration
        ) {
          return
        }
        if (progress.terminal) {
          // consumeJobProgress owns any result read it awaited. Stop only the
          // polling machinery so this broadcast cannot cancel a newer read.
          stopRuntimePoll(consoleId, runtime, { cancelResultRead: false })
        } else {
          scheduleConsolePoll(consoleId, runtime, nextQueryPollDelay(runtime, progress.retry_after_ms))
        }
      })
    }
  }
}

function stopRuntimePoll(
  consoleId: string,
  runtime: ConsoleRuntime,
  options: { cancelResultRead?: boolean } = {},
): void {
  const key = runtimeKey(runtime)
  if (!key) return
  if (options.cancelResultRead !== false) cancelResultRead(runtime)
  const pollKey = `query:${consoleId}:${key}`
  const timer = pollTimers.get(pollKey)
  if (timer) clearTimeout(timer)
  pollTimers.delete(pollKey)
  pollControllers.get(pollKey)?.abort()
  pollControllers.delete(pollKey)
  releasePollLease(key)
}

function onVisibilityChange(): void {
  const consoleId = activeConsoleId.value
  if (!consoleId) return
  stopConsolePoll(consoleId)
  resumeConsolePoll(consoleId)
}

async function onChangePage(offset: number): Promise<void> {
  const runtime = activeRuntime.value
  if (!runtime || !runtimeKey(runtime) || !runtime.result) return
  const loadedRows = runtime.result.loaded_rows ?? 0
  if (offset < loadedRows) {
    await fetchResult(runtime, offset)
    return
  }
  if (!runtime.result.has_more) return
  if (runtime.statementId) {
    // 已落 spool 的行随便翻;越过它就得重查一次数据库,而语句路径没有
    // job 的 `/jobs/{id}/pages` 那种无状态分页端点(阶段 A 范围线外)。
    // 如实说明,不假装能翻。
    runtime.error = t('sql.session_page_beyond_loaded')
    return
  }
  if (runtime.result.pagination_mode !== 'ordered_offset') {
    runtime.error = t('sql.pagination_order_required')
    return
  }
  if (writesBlocked.value) {
    runtime.error = t('license.writes_blocked')
    return
  }
  const consoleId = activeConsoleId.value
  if (!consoleId) return
  const originalJobId = runtime.jobId
  const originalRootJobId = runtime.rootJobId
  // 会话语句已在上面提前返回,能走到这里的必然是 job 路径。
  const requestJobId = runtime.rootJobId ?? runtime.jobId
  if (!requestJobId) return
  // Invalidate an in-flight progressive delta before changing the page/job.
  const requestGeneration = cancelResultRead(runtime)
  runtime.resultLoading = true
  runtime.error = null
  let keepLoadingForNewPage = false
  try {
    const page = await requestJobPage(requestJobId, offset)
    if (
      !isCurrentRuntime(consoleId, runtime) ||
      runtime.resultGeneration !== requestGeneration ||
      runtime.jobId !== originalJobId ||
      runtime.rootJobId !== originalRootJobId
    ) {
      return
    }
    if (page.cached) {
      await fetchResult(runtime, offset, false)
      return
    }
    stopRuntimePoll(consoleId, runtime)
    runtime.jobId = page.job_id
    runtime.status = 'pending'
    runtime.pageOffset = offset
    runtime.startedAt = null
    runtime.finishedAt = null
    runtime.timings = null
    runtime.progress = null
    runtime.unchangedPolls = 0
    runtime.networkFailures = 0
    runtime.resultLoading = true
    keepLoadingForNewPage = true
    startConsolePoll(consoleId, runtime)
  } catch (e) {
    if (isCurrentRuntime(consoleId, runtime) && runtime.resultGeneration === requestGeneration) {
      runtime.error = errorMessage(e)
    }
  } finally {
    if (!keepLoadingForNewPage && isCurrentRuntime(consoleId, runtime) && runtime.resultGeneration === requestGeneration) {
      runtime.resultLoading = false
    }
  }
}

async function loadHistory(): Promise<void> {
  historyLoading.value = true
  historyError.value = null
  try {
    historyItems.value = await listSqlHistory({
      datasource_id: historyDatasourceId.value || undefined,
      created_after: historyCreatedAfter(),
      limit: 100,
    })
  } catch (e) {
    historyError.value = errorMessage(e)
  } finally {
    historyLoading.value = false
  }
}

function historyCreatedAfter(): string | undefined {
  if (historyRange.value === 'all') return undefined
  const date = new Date()
  if (historyRange.value === 'today') date.setHours(0, 0, 0, 0)
  if (historyRange.value === '7d') date.setDate(date.getDate() - 7)
  return date.toISOString()
}

function applyHistory(item: SqlHistoryItem): void {
  applySqlToActiveConsole(item.sql, item.datasource_id)
  sidebarTab.value = 'consoles'
}

async function loadTemplates(): Promise<void> {
  templatesLoading.value = true
  templatesError.value = null
  try {
    templates.value = await listSqlTemplates({
      q: templateSearch.value.trim() || undefined,
    })
  } catch (e) {
    templatesError.value = errorMessage(e)
  } finally {
    templatesLoading.value = false
  }
}

function selectTemplate(template: SqlTemplate): void {
  selectedTemplate.value = template
  templateRenderError.value = null
  for (const key of Object.keys(templateValues)) delete templateValues[key]
  for (const variable of template.variables) templateValues[variable] = ''
  if (template.variables.length === 0) {
    applySqlToActiveConsole(template.sql_text, selectedDsId.value || null)
    sidebarTab.value = 'consoles'
  }
}

async function applySelectedTemplate(): Promise<void> {
  if (writesBlocked.value) {
    templateRenderError.value = t('license.writes_blocked')
    return
  }
  if (!selectedTemplate.value) return
  templateRenderError.value = null
  try {
    const rendered = await renderSqlTemplate(selectedTemplate.value.id, { ...templateValues })
    applySqlToActiveConsole(rendered.sql_text, selectedDsId.value || null)
    selectedTemplate.value = null
    sidebarTab.value = 'consoles'
  } catch (e) {
    templateRenderError.value = errorMessage(e)
  }
}

function openTemplateCreate(): void {
  if (writesBlocked.value) return
  Object.assign(templateForm, {
    id: null,
    name: '',
    description: '',
    category: 'general',
    sql_text: editorSql.value || 'SELECT * FROM {{table_name}} LIMIT 100',
    variables: extractVariables(editorSql.value).join(', '),
    project_id: '',
  })
  templateModalOpen.value = true
}

function openTemplateEdit(template: SqlTemplate): void {
  if (writesBlocked.value) return
  Object.assign(templateForm, {
    id: template.id,
    name: template.name,
    description: template.description ?? '',
    category: template.category,
    sql_text: template.sql_text,
    variables: template.variables.join(', '),
    project_id: template.project_id ?? '',
  })
  templateModalOpen.value = true
}

async function submitTemplateForm(): Promise<void> {
  if (writesBlocked.value) {
    templatesError.value = t('license.writes_blocked')
    return
  }
  if (!templateForm.name.trim() || !templateForm.sql_text.trim()) return
  templateSaving.value = true
  const payload: SqlTemplateCreateRequest = {
    name: templateForm.name.trim(),
    description: templateForm.description.trim() || null,
    category: templateForm.category.trim() || 'general',
    sql_text: templateForm.sql_text,
    variables: parseVariables(templateForm.variables),
    project_id: templateForm.project_id.trim() || null,
  }
  try {
    if (templateForm.id) await updateSqlTemplate(templateForm.id, payload)
    else await createSqlTemplate(payload)
    templateModalOpen.value = false
    await loadTemplates()
  } catch (e) {
    templatesError.value = errorMessage(e)
  } finally {
    templateSaving.value = false
  }
}

async function removeTemplate(template: SqlTemplate): Promise<void> {
  if (writesBlocked.value) return
  templatesError.value = null
  try {
    await deleteSqlTemplate(template.id)
    await loadTemplates()
    if (selectedTemplate.value?.id === template.id) selectedTemplate.value = null
  } catch (e) {
    templatesError.value = errorMessage(e)
  }
}

function applySqlToActiveConsole(sqlText: string, datasourceId: string | null): void {
  const consoleRow = activeConsole.value
  if (!consoleRow) return
  editorSql.value = sqlText
  if (datasourceId) selectedDsId.value = datasourceId
  patchLocalConsole(consoleRow.id, {
    sql: sqlText,
    datasource_id: datasourceId || selectedDsId.value || null,
  })
  scheduleConsolePatch(consoleRow.id, {
    sql: sqlText,
    datasource_id: datasourceId || selectedDsId.value || null,
  })
}

// ── 元数据浏览器 ────────────────────────────────────────────────
async function loadMetadataSchemas(refresh: boolean): Promise<void> {
  if (!metadataDsId.value) return
  if (refresh) clearSqlMetadataCache(metadataDsId.value)
  metadataLoading.value = true
  metadataError.value = null
  try {
    const items = await listMetadataSchemas(metadataDsId.value, refresh)
    metadataSchemas.value = items.map((schema) => ({
      schema,
      expanded: false,
      loading: false,
      error: null,
      tables: [],
    }))
  } catch (e) {
    metadataError.value = errorMessage(e)
  } finally {
    metadataLoading.value = false
  }
}

async function toggleSchema(node: MetadataSchemaNode, refresh = false): Promise<void> {
  if (node.expanded && !refresh) {
    node.expanded = false
    return
  }
  node.expanded = true
  if (node.tables.length > 0 && !refresh) return
  node.loading = true
  node.error = null
  try {
    const items = await listMetadataTables(metadataDsId.value, node.schema.name, refresh)
    node.tables = items.map((table) => ({
      table,
      expanded: false,
      loading: false,
      error: null,
      columns: [],
    }))
  } catch (e) {
    node.error = errorMessage(e)
  } finally {
    node.loading = false
  }
}

async function toggleTable(
  schemaName: string,
  node: MetadataTableNode,
  refresh = false,
): Promise<void> {
  if (node.expanded && !refresh) {
    node.expanded = false
    return
  }
  node.expanded = true
  if (node.columns.length > 0 && !refresh) return
  node.loading = true
  node.error = null
  try {
    node.columns = await listMetadataColumns(
      metadataDsId.value,
      schemaName,
      node.table.name,
      refresh,
    )
  } catch (e) {
    node.error = errorMessage(e)
  } finally {
    node.loading = false
  }
}

// 点表名:把 SELECT * FROM <schema>.<table> LIMIT 100 写进当前 console,并切到该数据源。
function selectTableIntoConsole(schemaName: string, node: MetadataTableNode): void {
  const qualified = schemaName ? `${schemaName}.${node.table.name}` : node.table.name
  applySqlToActiveConsole(`SELECT * FROM ${qualified} LIMIT 100`, metadataDsId.value || null)
  sidebarTab.value = 'consoles'
}

// ── 编辑器工具栏 ────────────────────────────────────────────────
async function onFormatSql(): Promise<void> {
  if (writesBlocked.value) {
    toolError.value = t('license.writes_blocked')
    return
  }
  const target = editorSqlTarget()
  if (!target?.sql.trim() || !selectedDs.value) return
  toolError.value = null
  toolBusy.value = 'format'
  try {
    const res = await formatSql(target.sql, selectedDs.value.db_type)
    replaceEditorSqlTarget(target, res.formatted_sql)
  } catch (e) {
    toolError.value = errorMessage(e)
  } finally {
    toolBusy.value = ''
  }
}

async function onExpandStar(): Promise<void> {
  if (writesBlocked.value) {
    toolError.value = t('license.writes_blocked')
    return
  }
  const target = editorSqlTarget()
  if (!target?.sql.trim() || !selectedDsId.value) return
  toolError.value = null
  toolBusy.value = 'expand'
  try {
    const res = await expandSqlStar(target.sql, selectedDsId.value)
    replaceEditorSqlTarget(target, res.expanded_sql)
  } catch (e) {
    // 缓存缺失(409 metadata_cache_missing)→ 提示先刷新元数据。
    if (e instanceof ApiError && e.code === 'metadata_cache_missing') {
      toolError.value = t('sql.expand_needs_metadata')
    } else {
      toolError.value = errorMessage(e)
    }
  } finally {
    toolBusy.value = ''
  }
}

async function onExplain(): Promise<void> {
  if (writesBlocked.value) {
    toolError.value = t('license.writes_blocked')
    return
  }
  const consoleRow = activeConsole.value
  const target = editorSqlTarget()
  if (!consoleRow || !selectedDsId.value || !target?.sql.trim()) return
  if (!explainSupported.value) return
  toolError.value = null
  toolBusy.value = 'explain'
  const runtime = runtimeFor(consoleRow.id)
  runtime.planError = null
  runtime.planResult = null
  runtime.planStatus = 'pending'
  runtime.resultTab = 'plan'
  // 新计划作废旧 AI 诊断(诊断锚在具体 plan 上)。
  resetAiDiagnose()
  await flushConsolePatch(consoleRow.id)
  try {
    const response = await explainSql({
      datasource_id: selectedDsId.value,
      console_id: consoleRow.id,
      sql: target.sql,
    })
    runtime.planJobId = response.job_id
    startPlanPoll(consoleRow.id)
    scrollResultIntoView()
  } catch (e) {
    runtime.planStatus = null
    runtime.planError = errorMessage(e)
  } finally {
    toolBusy.value = ''
  }
}

function applyGeneratedSql(sql: string): void {
  editorSql.value = sql
}

// SQL 体检(C-11):文本级 advisory findings 卡;若数据源支持 EXPLAIN,顺带触发一次
// 库内 EXPLAIN(行数估算落 Plan tab)—— 复用既有端点,不自造行数解析。
async function onPreflight(): Promise<void> {
  if (writesBlocked.value) {
    toolError.value = t('license.writes_blocked')
    return
  }
  const target = editorSqlTarget()
  if (!target?.sql.trim()) return
  toolError.value = null
  toolBusy.value = 'preflight'
  try {
    const res = await preflightSql(target.sql)
    preflightFindings.value = res.findings
    if (explainSupported.value) void onExplain()
  } catch (e) {
    toolError.value = errorMessage(e)
  } finally {
    toolBusy.value = ''
  }
}

// finding 优先按 code 走 i18n(zh/en 双语),缺 key 回退后端英文 message。
function preflightMessage(finding: SqlPreflightFinding): string {
  const key = `sql.preflight_finding.${finding.code}`
  return te(key) ? t(key) : finding.message
}

function resetAiDiagnose(): void {
  aiDiagnosis.value = null
  aiDiagnoseError.value = null
  aiDiagnoseDisabled.value = false
  aiDiagnoseBaseline.value = null
}

async function onDiagnoseSlowSql(): Promise<void> {
  if (writesBlocked.value) {
    aiDiagnoseError.value = t('license.writes_blocked')
    return
  }
  const runtime = activeRuntime.value
  const sql = runtime?.statement || editorSqlTarget()?.sql || ''
  if (!runtime || !selectedDsId.value || !sql.trim()) return
  resetAiDiagnose()
  aiDiagnoseBusy.value = true
  try {
    // 复用 Plan tab 已跑完的 explain job(有则带上,plan 进 AI 上下文;无则 AI 据结构+基线诊断)。
    const explainJobId = runtime.planStatus === 'success' ? runtime.planJobId : null
    const res = await diagnoseSlowSql(selectedDsId.value, {
      sql,
      explain_job_id: explainJobId,
    })
    if (!res.ok || !res.diagnosis) {
      aiDiagnoseError.value = t('sql.ai_diagnose_failed')
      return
    }
    aiDiagnosis.value = res.diagnosis
    aiDiagnoseBaseline.value = { available: res.baseline_available, runs: res.baseline_runs }
  } catch (e) {
    // AI 未启用 → 后端结构化 409 ai_disabled;给友好禁用提示。
    if (e instanceof ApiError && e.code === 'ai_disabled') {
      aiDiagnoseDisabled.value = true
    } else {
      aiDiagnoseError.value = errorMessage(e)
    }
  } finally {
    aiDiagnoseBusy.value = false
  }
}

function startPlanPoll(consoleId: string): void {
  void pollPlan(consoleId)
}

async function pollPlan(consoleId: string): Promise<void> {
  const runtime = runtimeFor(consoleId)
  if (!runtime.planJobId) return
  try {
    const job = await getJob(runtime.planJobId)
    runtime.planStatus = job.status
    if (job.status === 'success') {
      runtime.planResult = await getJobResult(runtime.planJobId, 0, DEFAULT_PAGE_SIZE)
    }
    if (TERMINAL.has(job.status)) {
      if (job.status !== 'success') {
        runtime.planError = job.error || t('jobs.error.sql_failed')
      }
      return
    }
  } catch (e) {
    runtime.planError = errorMessage(e)
    return
  }
  const timer = setTimeout(() => {
    void pollPlan(consoleId)
  }, POLL_MS)
  pollTimers.set(`plan:${consoleId}`, timer)
}

// ── 导出 ────────────────────────────────────────────────────────
function resetExportState(): void {
  exportError.value = null
  exportReady.value = null
}

async function onCreateExport(format: ExportFormat): Promise<void> {
  if (writesBlocked.value) {
    exportError.value = t('license.writes_blocked')
    return
  }
  const source = exportableSource.value
  exportMenuOpen.value = false
  if (!source || exportBusy.value) return
  resetExportState()
  exportBusy.value = true
  try {
    const res =
      source.kind === 'statement'
        ? await createStatementExport(source.id, format)
        : await createExport(source.id, format)
    await pollExportJob(res.job_id, res.download_token, res.filename)
  } catch (e) {
    exportBusy.value = false
    if (e instanceof ApiError && e.status === 429) {
      exportError.value = t('sql.export_rate_limited')
    } else if (e instanceof ApiError && (e.status === 404 || e.status === 409)) {
      exportError.value = t('sql.export_source_gone')
    } else {
      exportError.value = errorMessage(e)
    }
  }
}

async function pollExportJob(
  exportJobId: string,
  token: string,
  filename: string,
): Promise<void> {
  try {
    const job = await getJob(exportJobId)
    if (job.status === 'success') {
      exportBusy.value = false
      exportReady.value = { token, filename }
      return
    }
    if (TERMINAL.has(job.status)) {
      exportBusy.value = false
      exportError.value = job.error || t('sql.export_failed')
      return
    }
  } catch (e) {
    exportBusy.value = false
    exportError.value = errorMessage(e)
    return
  }
  const timer = setTimeout(() => {
    exportPollTimers.delete(timer)
    void pollExportJob(exportJobId, token, filename)
  }, POLL_MS)
  exportPollTimers.add(timer)
}

async function onDownloadExport(): Promise<void> {
  if (writesBlocked.value) {
    exportError.value = t('license.writes_blocked')
    return
  }
  if (!exportReady.value) return
  const { token, filename } = exportReady.value
  try {
    await downloadExport(token, filename)
    exportReady.value = null
  } catch (e) {
    // 一次性 token 用过/过期 → 410;提示重新导出。
    exportReady.value = null
    if (e instanceof ApiError && e.status === 410) {
      exportError.value = t('sql.export_token_spent')
    } else {
      exportError.value = errorMessage(e)
    }
  }
}

function scrollResultIntoView(): void {
  void nextTick(() => {
    resultPanel.value?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
  })
}

function onEditorMount(editor: monaco.editor.IStandaloneCodeEditor): void {
  sqlEditor = editor
}

function elapsedSeconds(runtime: ConsoleRuntime): string {
  if (!runtime.startedAt) return '0.0'
  return (((runtime.finishedAt ?? nowMs.value) - runtime.startedAt) / 1000).toFixed(1)
}

function formatTiming(value: number | null | undefined): string {
  if (value == null) return '-'
  return value < 1000 ? `${value}ms` : `${(value / 1000).toFixed(2)}s`
}

function parseTimeMs(value: string | null | undefined): number | null {
  if (!value) return null
  const ms = Date.parse(value)
  return Number.isNaN(ms) ? null : ms
}

function formatDate(iso: string | null): string {
  if (!iso) return '-'
  try {
    return new Date(iso).toLocaleString()
  } catch {
    return iso
  }
}

function extractVariables(sqlText: string): string[] {
  const matches = sqlText.matchAll(/\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}/g)
  return [...new Set([...matches].map((match) => match[1]))]
}

function parseVariables(value: string): string[] {
  const typed = value
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean)
  return typed.length > 0 ? typed : extractVariables(templateForm.sql_text)
}
</script>

<template>
  <div class="flex h-full min-h-0 min-w-0 max-w-full overflow-hidden chrome-bg-main">
    <aside class="w-[21rem] shrink-0 border-r chrome-border chrome-bg-panel flex flex-col min-h-0">
      <div class="px-4 py-3 border-b chrome-border-subtle">
        <div class="text-xs uppercase tracking-wider chrome-text-muted font-medium">
          {{ t('nav.projects') }} / {{ projectId.slice(0, 8) }}
        </div>
        <div class="text-section font-semibold chrome-text-heading mt-1">
          {{ t('sql.workspace') }}
        </div>
      </div>

      <div class="grid grid-cols-4 gap-1 p-2 border-b chrome-border-subtle">
        <button
          type="button"
          class="chrome-btn-ghost w-full"
          :class="sidebarTab === 'consoles' && 'chrome-accent-light-bg chrome-accent'"
          :title="t('sql.tab_consoles')"
          @click="sidebarTab = 'consoles'"
        >
          <FileText class="w-4 h-4" />
        </button>
        <button
          type="button"
          class="chrome-btn-ghost w-full"
          :class="sidebarTab === 'history' && 'chrome-accent-light-bg chrome-accent'"
          :title="t('sql.tab_history')"
          @click="sidebarTab = 'history'"
        >
          <History class="w-4 h-4" />
        </button>
        <button
          type="button"
          class="chrome-btn-ghost w-full"
          :class="sidebarTab === 'templates' && 'chrome-accent-light-bg chrome-accent'"
          :title="t('sql.tab_templates')"
          @click="sidebarTab = 'templates'"
        >
          <BookOpen class="w-4 h-4" />
        </button>
        <button
          type="button"
          class="chrome-btn-ghost w-full"
          :class="sidebarTab === 'metadata' && 'chrome-accent-light-bg chrome-accent'"
          :title="t('sql.tab_metadata')"
          @click="sidebarTab = 'metadata'"
        >
          <Database class="w-4 h-4" />
        </button>
      </div>

      <div v-if="sidebarTab === 'consoles'" class="flex-1 min-h-0 flex flex-col">
        <div class="flex items-center gap-2 px-3 py-2 border-b chrome-border-subtle">
          <span class="text-xs font-medium uppercase tracking-wider chrome-text-muted flex-1">
            {{ t('sql.tab_consoles') }}
          </span>
          <button
            type="button"
            class="chrome-btn-ghost"
            :disabled="writesBlocked"
            :title="writesBlocked ? t('license.writes_blocked') : t('sql.new_console')"
            @click="createConsole"
          >
            <Plus class="w-4 h-4" />
          </button>
        </div>
        <div v-if="consoleError" class="px-3 py-2 text-xs text-red-600 dark:text-red-400">
          {{ consoleError }}
        </div>
        <div v-if="consolesLoading" class="p-4 chrome-text-muted text-sm">
          <LoadingDots />
        </div>
        <div v-else-if="sortedConsoles.length === 0" class="p-4">
          <button
            type="button"
            class="chrome-btn-primary w-full justify-center"
            :disabled="writesBlocked"
            :title="writesBlocked ? t('license.writes_blocked') : ''"
            @click="createConsole"
          >
            <Plus class="w-4 h-4" />
            {{ t('sql.new_console') }}
          </button>
        </div>
        <div v-else class="flex-1 min-h-0 overflow-auto p-2 space-y-1">
          <div
            v-for="consoleRow in sortedConsoles"
            :key="consoleRow.id"
            class="group rounded-card border px-2 py-2 cursor-pointer transition-colors"
            :class="
              activeConsoleId === consoleRow.id
                ? 'chrome-border chrome-accent-light-bg'
                : 'border-transparent hover:chrome-bg-elevated'
            "
            @click="activeConsoleId = consoleRow.id"
          >
            <div class="flex items-center gap-2 min-w-0">
              <Pin v-if="consoleRow.pinned" class="w-3.5 h-3.5 chrome-accent shrink-0" />
              <FileText v-else class="w-3.5 h-3.5 chrome-text-muted shrink-0" />
              <input
                v-if="renameConsoleId === consoleRow.id"
                v-model="renameConsoleDraft"
                class="chrome-input flex-1 min-w-0 py-1 text-xs"
                :disabled="writesBlocked"
                :title="writesBlocked ? t('license.writes_blocked') : ''"
                @keyup.enter="submitRename"
                @keyup.esc="renameConsoleId = null"
                @click.stop
              />
              <div v-else class="flex-1 min-w-0 truncate text-sm chrome-text-heading">
                {{ consoleRow.name }}
              </div>
              <button
                type="button"
                class="chrome-btn-ghost opacity-0 group-hover:opacity-100"
                :disabled="writesBlocked"
                :title="writesBlocked ? t('license.writes_blocked') : consoleRow.pinned ? t('sql.unpin_console') : t('sql.pin_console')"
                @click.stop="togglePinned(consoleRow)"
              >
                <PinOff v-if="consoleRow.pinned" class="w-3.5 h-3.5" />
                <Pin v-else class="w-3.5 h-3.5" />
              </button>
              <button
                type="button"
                class="chrome-btn-ghost opacity-0 group-hover:opacity-100"
                :disabled="writesBlocked"
                :title="writesBlocked ? t('license.writes_blocked') : t('common.edit')"
                @click.stop="startRename(consoleRow)"
              >
                <Pencil class="w-3.5 h-3.5" />
              </button>
              <button
                type="button"
                class="chrome-btn-ghost opacity-0 group-hover:opacity-100"
                :disabled="writesBlocked"
                :title="writesBlocked ? t('license.writes_blocked') : t('common.delete')"
                @click.stop="deleteConsole(consoleRow.id)"
              >
                <Trash2 class="w-3.5 h-3.5" />
              </button>
            </div>
            <div class="mt-1 text-[11px] chrome-text-muted font-mono truncate">
              {{ consoleRow.sql || 'SELECT' }}
            </div>
          </div>
        </div>
      </div>

      <div v-else-if="sidebarTab === 'history'" class="flex-1 min-h-0 flex flex-col">
        <div class="p-3 border-b chrome-border-subtle space-y-2">
          <div class="flex items-center gap-2">
            <select v-model="historyDatasourceId" class="chrome-input flex-1 text-xs">
              <option value="">{{ t('sql.history_all_datasources') }}</option>
              <option v-for="ds in datasources" :key="ds.id" :value="ds.id">
                {{ ds.name }}
              </option>
            </select>
            <select v-model="historyRange" class="chrome-input text-xs">
              <option value="all">{{ t('sql.range_all') }}</option>
              <option value="today">{{ t('sql.range_today') }}</option>
              <option value="7d">{{ t('sql.range_7d') }}</option>
            </select>
          </div>
        </div>
        <div v-if="historyError" class="px-3 py-2 text-xs text-red-600 dark:text-red-400">
          {{ historyError }}
        </div>
        <div v-if="historyLoading" class="p-4 chrome-text-muted text-sm">
          <LoadingDots />
        </div>
        <div v-else class="flex-1 min-h-0 overflow-auto p-2 space-y-1">
          <button
            v-for="item in historyItems"
            :key="item.sql_hash"
            type="button"
            class="w-full text-left rounded-card px-3 py-2 hover:chrome-bg-elevated"
            @click="applyHistory(item)"
          >
            <div class="flex items-center gap-2">
              <JobStatusBadge :status="item.status as JobStatus" />
              <span class="text-xs chrome-text-muted">{{ item.datasource_name || item.datasource_id }}</span>
            </div>
            <div class="mt-1 font-mono text-xs chrome-text-heading truncate">{{ item.sql }}</div>
            <div class="mt-1 text-[11px] chrome-text-muted">{{ formatDate(item.created_at) }}</div>
          </button>
          <div v-if="historyItems.length === 0" class="p-4 chrome-text-muted text-sm">
            {{ t('sql.history_empty') }}
          </div>
        </div>
      </div>

      <div v-else-if="sidebarTab === 'templates'" class="flex-1 min-h-0 flex flex-col">
        <div class="p-3 border-b chrome-border-subtle space-y-2">
          <div class="flex items-center gap-2">
            <div class="relative flex-1">
              <Search class="absolute left-2 top-1/2 -translate-y-1/2 w-3.5 h-3.5 chrome-text-muted" />
              <input v-model="templateSearch" class="chrome-input w-full pl-7 text-xs" :placeholder="t('common.search_placeholder')" />
            </div>
            <button
              v-if="isAdmin"
              type="button"
              class="chrome-btn-ghost"
              :disabled="writesBlocked"
              :title="writesBlocked ? t('license.writes_blocked') : t('sql.template_new')"
              @click="openTemplateCreate"
            >
              <Plus class="w-4 h-4" />
            </button>
          </div>
        </div>
        <div v-if="templatesError" class="px-3 py-2 text-xs text-red-600 dark:text-red-400">
          {{ templatesError }}
        </div>
        <div v-if="templatesLoading" class="p-4 chrome-text-muted text-sm">
          <LoadingDots />
        </div>
        <div v-else class="flex-1 min-h-0 overflow-auto p-2">
          <div v-for="[category, list] in templateCategories" :key="category" class="mb-3">
            <div class="px-2 py-1 text-[11px] uppercase tracking-wider chrome-text-muted font-medium">
              {{ category }}
            </div>
            <button
              v-for="template in list"
              :key="template.id"
              type="button"
              class="w-full text-left rounded-card px-3 py-2 hover:chrome-bg-elevated"
              @click="selectTemplate(template)"
            >
              <div class="flex items-center gap-2">
                <span class="flex-1 truncate text-sm chrome-text-heading">{{ template.name }}</span>
                <button
                  v-if="isAdmin"
                  type="button"
                  class="chrome-btn-ghost"
                  :disabled="writesBlocked"
                  :title="writesBlocked ? t('license.writes_blocked') : t('common.edit')"
                  @click.stop="openTemplateEdit(template)"
                >
                  <Pencil class="w-3.5 h-3.5" />
                </button>
                <button
                  v-if="isAdmin"
                  type="button"
                  class="chrome-btn-ghost"
                  :disabled="writesBlocked"
                  :title="writesBlocked ? t('license.writes_blocked') : t('common.delete')"
                  @click.stop="removeTemplate(template)"
                >
                  <Trash2 class="w-3.5 h-3.5" />
                </button>
              </div>
              <div class="mt-1 text-xs chrome-text-muted line-clamp-2">
                {{ template.description || template.sql_text }}
              </div>
            </button>
          </div>
          <div v-if="templates.length === 0" class="p-4 chrome-text-muted text-sm">
            {{ t('sql.template_empty') }}
          </div>
        </div>

        <div v-if="selectedTemplate && selectedTemplate.variables.length > 0" class="border-t chrome-border-subtle p-3 space-y-2">
          <div class="text-xs font-medium chrome-text-heading">{{ selectedTemplate.name }}</div>
          <label v-for="variable in selectedTemplate.variables" :key="variable" class="block">
            <span class="block text-[11px] chrome-text-muted mb-1">{{ variable }}</span>
            <input v-model="templateValues[variable]" class="chrome-input w-full text-xs" />
          </label>
          <div v-if="templateRenderError" class="text-xs text-red-600 dark:text-red-400">
            {{ templateRenderError }}
          </div>
          <button
            type="button"
            class="chrome-btn-primary w-full justify-center"
            :disabled="writesBlocked"
            :title="writesBlocked ? t('license.writes_blocked') : ''"
            @click="applySelectedTemplate"
          >
            <Save class="w-3.5 h-3.5" />
            {{ t('sql.template_insert') }}
          </button>
        </div>
      </div>

      <div v-else-if="sidebarTab === 'metadata'" class="flex-1 min-h-0 flex flex-col">
        <div class="p-3 border-b chrome-border-subtle space-y-2">
          <div class="flex items-center gap-2">
            <select v-model="metadataDsId" class="chrome-input flex-1 text-xs">
              <option v-if="datasources.length === 0" disabled value="">
                {{ t('sql.no_datasource') }}
              </option>
              <option v-for="ds in datasources" :key="ds.id" :value="ds.id">
                {{ ds.name }} ({{ ds.db_type }})
              </option>
            </select>
            <button
              type="button"
              class="chrome-btn-ghost"
              :title="t('sql.metadata_refresh')"
              :disabled="!metadataDsId || metadataLoading"
              @click="loadMetadataSchemas(true)"
            >
              <RefreshCw class="w-4 h-4" :class="metadataLoading && 'animate-spin'" />
            </button>
          </div>
          <div
            v-if="!metadataToolsSupported && metadataDsId"
            class="text-[11px] chrome-text-muted"
          >
            {{ t('sql.metadata_unsupported_db') }}
          </div>
        </div>
        <div v-if="metadataError" class="px-3 py-2 text-xs text-red-600 dark:text-red-400">
          {{ metadataError }}
        </div>
        <div v-if="metadataLoading && metadataSchemas.length === 0" class="p-4 chrome-text-muted text-sm">
          <LoadingDots />
        </div>
        <div v-else class="flex-1 min-h-0 overflow-auto p-2 text-sm">
          <div v-if="!metadataDsId" class="p-4 chrome-text-muted">
            {{ t('sql.metadata_pick_ds') }}
          </div>
          <div v-else-if="metadataSchemas.length === 0 && !metadataError" class="p-4 chrome-text-muted">
            {{ t('sql.metadata_empty') }}
          </div>
          <div v-for="schemaNode in metadataSchemas" :key="schemaNode.schema.name">
            <button
              type="button"
              class="w-full flex items-center gap-1.5 px-2 py-1.5 rounded-card hover:chrome-bg-elevated text-left"
              @click="toggleSchema(schemaNode)"
            >
              <ChevronDown v-if="schemaNode.expanded" class="w-3.5 h-3.5 chrome-text-muted shrink-0" />
              <ChevronRight v-else class="w-3.5 h-3.5 chrome-text-muted shrink-0" />
              <Database class="w-3.5 h-3.5 chrome-text-muted shrink-0" />
              <span class="truncate chrome-text-heading">{{ schemaNode.schema.name }}</span>
            </button>
            <div v-if="schemaNode.expanded" class="pl-4">
              <div v-if="schemaNode.loading" class="px-2 py-1 chrome-text-muted">
                <LoadingDots />
              </div>
              <div v-else-if="schemaNode.error" class="px-2 py-1 text-xs text-red-600 dark:text-red-400">
                {{ schemaNode.error }}
              </div>
              <div
                v-else-if="schemaNode.tables.length === 0"
                class="px-2 py-1 text-xs chrome-text-muted"
              >
                {{ t('sql.metadata_no_tables') }}
              </div>
              <div v-for="tableNode in schemaNode.tables" :key="tableNode.table.name">
                <div class="group flex items-center gap-1.5 px-2 py-1.5 rounded-card hover:chrome-bg-elevated">
                  <button
                    type="button"
                    class="shrink-0"
                    :title="t('sql.metadata_expand_columns')"
                    @click="toggleTable(schemaNode.schema.name, tableNode)"
                  >
                    <ChevronDown v-if="tableNode.expanded" class="w-3.5 h-3.5 chrome-text-muted" />
                    <ChevronRight v-else class="w-3.5 h-3.5 chrome-text-muted" />
                  </button>
                  <Table2 class="w-3.5 h-3.5 chrome-text-muted shrink-0" />
                  <button
                    type="button"
                    class="flex-1 min-w-0 text-left truncate chrome-text-heading"
                    :title="t('sql.metadata_select_table')"
                    @click="selectTableIntoConsole(schemaNode.schema.name, tableNode)"
                  >
                    {{ tableNode.table.name }}
                  </button>
                  <span
                    v-if="tableNode.table.table_type && tableNode.table.table_type !== 'BASE TABLE'"
                    class="text-[10px] chrome-text-muted uppercase shrink-0"
                  >
                    {{ tableNode.table.table_type }}
                  </span>
                </div>
                <div v-if="tableNode.expanded" class="pl-6">
                  <div v-if="tableNode.loading" class="px-2 py-1 chrome-text-muted">
                    <LoadingDots />
                  </div>
                  <div v-else-if="tableNode.error" class="px-2 py-1 text-xs text-red-600 dark:text-red-400">
                    {{ tableNode.error }}
                  </div>
                  <div
                    v-for="column in tableNode.columns"
                    :key="column.name"
                    class="flex items-center gap-1.5 px-2 py-1 text-xs"
                  >
                    <Key v-if="column.primary_key" class="w-3 h-3 chrome-accent shrink-0" :title="t('sql.metadata_pk')" />
                    <span v-else class="w-3 shrink-0" />
                    <span class="chrome-text-heading truncate" :title="column.comment ?? ''">
                      {{ column.name }}
                    </span>
                    <span class="chrome-text-muted font-mono">{{ column.type }}</span>
                    <span v-if="column.nullable" class="text-[10px] chrome-text-muted">NULL</span>
                    <span v-else class="text-[10px] chrome-text-muted">NOT NULL</span>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </aside>

    <main class="flex-1 min-w-0 max-w-full overflow-hidden flex flex-col h-full">
      <div class="flex flex-wrap items-center gap-3 px-5 py-3 border-b chrome-border chrome-bg-panel">
        <div class="min-w-0">
          <div class="text-sm font-medium chrome-text-heading truncate">
            {{ activeConsole?.name || t('sql.no_console') }}
          </div>
          <div class="text-[11px] chrome-text-muted font-mono">
            {{ activeConsoleId ? activeConsoleId.slice(0, 8) : '-' }}
          </div>
        </div>
        <div class="flex-1" />
        <select
          id="sql-datasource"
          v-model="selectedDsId"
          class="chrome-input min-w-[12rem] max-w-full"
          :disabled="writesBlocked || dsQuery.isLoading.value || datasources.length === 0 || editorReadOnly"
          :aria-label="t('sql.datasource')"
        >
          <option v-if="datasources.length === 0" disabled value="">
            {{ t('sql.no_datasource') }}
          </option>
          <option v-for="ds in datasources" :key="ds.id" :value="ds.id">
            {{ ds.name }} ({{ ds.db_type }} · {{ ds.environment }})
          </option>
        </select>
        <div class="flex items-center gap-1.5 shrink-0">
          <label for="sql-page-size" class="text-xs chrome-text-muted whitespace-nowrap">
            {{ t('sql.page_size') }}
          </label>
          <select
            id="sql-page-size"
            v-model.number="pageSizeSelection"
            class="chrome-input w-[5.5rem]"
            :disabled="writesBlocked || editorReadOnly"
            :title="t('sql.page_size_hint')"
          >
            <option v-for="size in PAGE_SIZE_OPTIONS" :key="size" :value="size">
              {{ size.toLocaleString() }}
            </option>
          </select>
        </div>
        <div class="flex items-center gap-1.5 shrink-0">
          <label for="sql-max-rows" class="text-xs chrome-text-muted whitespace-nowrap">
            {{ t('sql.max_rows') }}
          </label>
          <select
            id="sql-max-rows"
            v-model="maxRowsSelection"
            class="chrome-input w-[6.5rem]"
            :disabled="writesBlocked || editorReadOnly"
            :title="t('sql.max_rows_hint')"
          >
            <option v-for="limit in QUERY_MAX_ROWS_OPTIONS" :key="limit" :value="String(limit)">
              {{ limit.toLocaleString() }}
            </option>
            <option value="custom">{{ t('sql.max_rows_custom') }}</option>
          </select>
          <input
            v-if="maxRowsSelection === 'custom'"
            v-model.number="customMaxRows"
            type="number"
            min="1"
            :max="QUERY_MAX_ROWS_LIMIT"
            step="100"
            class="chrome-input w-24"
            :aria-label="t('sql.max_rows_custom_input')"
            :disabled="writesBlocked || editorReadOnly"
          />
        </div>
        <button
          v-if="!editorReadOnly"
          type="button"
          class="chrome-btn-primary"
          data-testid="sql-execute"
          @click="onExecute"
          :disabled="writesBlocked || !activeConsole || !selectedDsId || !editorSql.trim() || !!unsupportedDb || queryMaxRows === null"
          :title="writesBlocked ? t('license.writes_blocked') : unsupportedDb ? t('sql.unsupported_db_error', { db: unsupportedDb }) : ''"
        >
          <Play class="w-3.5 h-3.5" />
          {{ t('sql.execute') }}
        </button>
        <button
          v-else
          type="button"
          class="chrome-btn-secondary"
          data-testid="sql-cancel"
          @click="onCancel"
          :disabled="writesBlocked || activeRuntime?.cancelling"
          :title="
            writesBlocked
              ? t('license.writes_blocked')
              : activeSession?.serverCancel === 'degraded'
                ? t('sql.session_cancel_degraded_hint')
                : ''
          "
        >
          <Square class="w-3.5 h-3.5" />
          {{ activeRuntime?.cancelling ? t('sql.cancelling') : t('sql.cancel') }}
        </button>
      </div>

      <ConsoleSessionBar
        :session="activeSession"
        :taken-over="sessionTakenOver"
        :lost="sessionLost"
        :busy="sessionBusy"
        @reconnect="onReconnectSession"
        @close="onCloseSession"
      />

      <div
        v-if="unsupportedDb"
        class="flex items-center gap-2 px-5 py-2 text-xs border-b chrome-border-subtle"
        style="background-color: rgb(180 83 9 / 0.10); color: rgb(180 83 9);"
      >
        <AlertTriangle class="w-3.5 h-3.5 shrink-0" />
        <span>{{ t('sql.unsupported_db_warn', { db: unsupportedDb }) }}</span>
      </div>

      <div v-if="!activeConsole" class="flex-1 grid place-items-center">
        <button
          type="button"
          class="chrome-btn-primary"
          :disabled="writesBlocked"
          :title="writesBlocked ? t('license.writes_blocked') : ''"
          @click="createConsole"
        >
          <Plus class="w-4 h-4" />
          {{ t('sql.new_console') }}
        </button>
      </div>
      <template v-else>
        <div
          data-testid="sql-editor-panel"
          class="h-[38%] min-h-[10rem] shrink-0 min-w-0 max-w-full overflow-hidden border-b chrome-border relative"
        >
          <SqlEditor
            :key="activeConsole.id"
            v-model="editorSql"
            :datasource-id="selectedDsId"
            :db-type="selectedDs?.db_type"
            :default-schema="selectedDs?.database ?? undefined"
            :path="`sql-console-${activeConsole.id}.sql`"
            :theme="editorTheme"
            :read-only="editorReadOnly || writesBlocked"
            @mount="onEditorMount"
            @execute="onExecute"
          />
        </div>

        <div class="flex items-center gap-2 px-5 py-2 border-b chrome-border-subtle">
          <button
            type="button"
            class="chrome-btn-secondary"
            :disabled="writesBlocked || !editorSql.trim() || !toolsSupported || editorReadOnly || toolBusy !== ''"
            :title="writesBlocked ? t('license.writes_blocked') : !toolsSupported ? t('sql.tools_unsupported_db') : t('sql.tool_format_hint')"
            @click="onFormatSql"
          >
            <AlignLeft class="w-3.5 h-3.5" />
            {{ toolBusy === 'format' ? t('common.submitting') : t('sql.tool_format') }}
          </button>
          <button
            type="button"
            class="chrome-btn-secondary"
            :disabled="writesBlocked || !editorSql.trim() || !toolsSupported || editorReadOnly || toolBusy !== ''"
            :title="writesBlocked ? t('license.writes_blocked') : !toolsSupported ? t('sql.tools_unsupported_db') : t('sql.tool_expand_hint')"
            @click="onExpandStar"
          >
            <Asterisk class="w-3.5 h-3.5" />
            {{ toolBusy === 'expand' ? t('common.submitting') : t('sql.tool_expand') }}
          </button>
          <button
            type="button"
            class="chrome-btn-secondary"
            :disabled="writesBlocked || !editorSql.trim() || !explainSupported || editorReadOnly || toolBusy !== ''"
            :title="
              writesBlocked
                ? t('license.writes_blocked')
                : !toolsSupported
                ? t('sql.tools_unsupported_db')
                : !explainSupported
                  ? t('sql.explain_not_allowed')
                  : t('sql.tool_explain_hint')
            "
            @click="onExplain"
          >
            <Network class="w-3.5 h-3.5" />
            {{ toolBusy === 'explain' ? t('common.submitting') : t('sql.tool_explain') }}
          </button>
          <button
            type="button"
            class="chrome-btn-secondary"
            :disabled="writesBlocked || !selectedDsId || editorReadOnly"
            :title="writesBlocked ? t('license.writes_blocked') : t('sql.ai_generate_hint')"
            @click="aiPanelOpen = true"
          >
            <Sparkles class="w-3.5 h-3.5" />
            {{ t('sql.ai_generate') }}
          </button>
          <button
            type="button"
            class="chrome-btn-secondary"
            :disabled="writesBlocked || !editorSql.trim() || editorReadOnly || toolBusy !== ''"
            :title="writesBlocked ? t('license.writes_blocked') : t('sql.tool_preflight_hint')"
            @click="onPreflight"
          >
            <ShieldCheck class="w-3.5 h-3.5" />
            {{ toolBusy === 'preflight' ? t('common.submitting') : t('sql.tool_preflight') }}
          </button>
          <div class="flex-1" />
          <span
            v-if="!toolError"
            class="hidden xl:inline text-[11px] chrome-text-muted truncate max-w-[24rem]"
            :title="t('sql.metadata_editor_hint')"
          >
            {{ t('sql.metadata_editor_hint') }}
          </span>
          <span v-if="toolError" class="text-xs text-red-600 dark:text-red-400 truncate max-w-[24rem]" :title="toolError">
            {{ toolError }}
          </span>
        </div>

        <!-- SQL 体检结果卡(C-11 advisory:只提示不拦截)-->
        <div v-if="preflightFindings" class="px-5 py-2 border-b chrome-border-subtle">
          <div class="rounded-card border chrome-border-subtle chrome-bg-elevated p-3 text-xs space-y-2">
            <div class="flex items-center gap-2">
              <ShieldCheck class="w-3.5 h-3.5 chrome-accent shrink-0" />
              <span class="font-medium chrome-text-heading flex-1">{{ t('sql.preflight_title') }}</span>
              <button type="button" class="chrome-btn-ghost p-0.5" :title="t('common.close')" @click="preflightFindings = null">
                <X class="w-3.5 h-3.5" />
              </button>
            </div>
            <p v-if="preflightFindings.length === 0" class="chrome-text-muted flex items-center gap-1.5">
              <ShieldCheck class="w-3.5 h-3.5 text-emerald-600 dark:text-emerald-400 shrink-0" />
              {{ t('sql.preflight_clean') }}
            </p>
            <ul v-else class="space-y-1.5">
              <li v-for="f in preflightFindings" :key="f.code" class="flex items-start gap-2">
                <AlertTriangle
                  v-if="f.severity === 'warning'"
                  class="w-3.5 h-3.5 shrink-0 mt-0.5 text-amber-600 dark:text-amber-400"
                />
                <Info v-else class="w-3.5 h-3.5 shrink-0 mt-0.5 chrome-text-muted" />
                <span class="chrome-text-normal">{{ preflightMessage(f) }}</span>
              </li>
            </ul>
            <p v-if="explainSupported" class="chrome-text-muted pt-0.5">{{ t('sql.preflight_explain_hint') }}</p>
          </div>
        </div>

        <div
          ref="resultPanel"
          data-testid="sql-result-panel"
          class="flex-1 min-h-0 flex flex-col overflow-hidden"
        >
          <div class="flex items-center justify-between gap-3 px-5 py-2 border-b chrome-border-subtle text-xs">
            <div class="flex items-center gap-1 shrink-0">
              <button
                type="button"
                class="chrome-tab"
                :class="(activeRuntime?.resultTab ?? 'result') === 'result' && 'chrome-accent-light-bg chrome-accent'"
                @click="activeRuntime && (activeRuntime.resultTab = 'result')"
              >
                <ListTree class="w-3.5 h-3.5" />
                {{ t('sql.result') }}
              </button>
              <button
                type="button"
                class="chrome-tab"
                :class="activeRuntime?.resultTab === 'plan' && 'chrome-accent-light-bg chrome-accent'"
                @click="activeRuntime && (activeRuntime.resultTab = 'plan')"
              >
                <Network class="w-3.5 h-3.5" />
                {{ t('sql.tab_plan') }}
              </button>
              <button
                type="button"
                class="chrome-tab"
                :class="activeRuntime?.resultTab === 'stats' && 'chrome-accent-light-bg chrome-accent'"
                @click="activeRuntime && (activeRuntime.resultTab = 'stats')"
              >
                <Gauge class="w-3.5 h-3.5" />
                {{ t('sql.tab_stats') }}
              </button>
            </div>
            <div class="flex items-center gap-3 min-w-0">
              <span class="chrome-text-muted truncate">{{ statusSummary }}</span>
              <span
                v-if="sessionPartialHint"
                data-testid="sql-session-partial"
                class="shrink-0 text-amber-700 dark:text-amber-300"
              >
                {{ sessionPartialHint }}
              </span>
              <div class="relative">
                <button
                  type="button"
                  class="chrome-btn-secondary"
                  :disabled="writesBlocked || !exportableSource || exportBusy"
                  :title="writesBlocked ? t('license.writes_blocked') : !exportableSource ? t('sql.export_needs_success') : t('sql.export')"
                  @click="exportMenuOpen = !exportMenuOpen"
                >
                  <Download class="w-3.5 h-3.5" />
                  {{ exportBusy ? t('sql.export_running') : t('sql.export') }}
                </button>
                <div
                  v-if="exportMenuOpen && exportableSource"
                  class="absolute right-0 mt-1 z-20 w-32 rounded-card border chrome-border chrome-bg-panel shadow-lg py-1"
                >
                  <button
                    v-for="fmt in EXPORT_FORMATS"
                    :key="fmt"
                    type="button"
                    class="w-full text-left px-3 py-1.5 text-sm hover:chrome-bg-elevated chrome-text-heading uppercase"
                    @click="onCreateExport(fmt)"
                  >
                    {{ fmt }}
                  </button>
                </div>
              </div>
            </div>
          </div>

          <div
            v-if="activeRuntimes.length > 1"
            data-testid="sql-statement-results"
            class="flex items-center gap-1 px-5 py-1.5 border-b chrome-border-subtle overflow-x-auto"
          >
            <button
              v-for="(runtime, index) in activeRuntimes"
              :key="`${runtime.statementIndex}:${runtime.jobId ?? 'pending'}`"
              type="button"
              class="chrome-tab shrink-0 max-w-48"
              :class="activeRuntime === runtime && 'chrome-accent-light-bg chrome-accent'"
              :title="runtime.statement"
              @click="selectRuntime(index)"
            >
              <span class="truncate">{{ t('sql.statement_result', { index: index + 1 }) }}</span>
              <JobStatusBadge v-if="runtime.status" :status="runtime.status" />
            </button>
          </div>

          <div
            v-if="exportError"
            class="flex items-center gap-2 px-5 py-2 border-b chrome-border-subtle text-xs"
            style="background-color: rgb(239 68 68 / 0.08); color: rgb(185 28 28);"
          >
            <AlertTriangle class="w-3.5 h-3.5 shrink-0" />
            <span class="flex-1">{{ exportError }}</span>
          </div>
          <div
            v-if="exportReady"
            class="flex items-center gap-2 px-5 py-2 border-b chrome-border-subtle text-xs chrome-accent-light-bg"
          >
            <Download class="w-3.5 h-3.5 shrink-0 chrome-accent" />
            <span class="flex-1 chrome-text-heading">{{ t('sql.export_ready', { file: exportReady.filename }) }}</span>
            <button
              type="button"
              class="chrome-btn-primary py-1"
              :disabled="writesBlocked"
              :title="writesBlocked ? t('license.writes_blocked') : ''"
              @click="onDownloadExport"
            >
              {{ t('sql.export_download') }}
            </button>
          </div>
          <div
            v-if="limitScanWarning"
            data-testid="sql-output-limit-warning"
            class="flex items-start gap-2 px-5 py-2 border-b chrome-border-subtle text-xs"
            style="background-color: rgb(245 158 11 / 0.10); color: rgb(180 83 9);"
          >
            <AlertTriangle class="w-3.5 h-3.5 shrink-0 mt-0.5" />
            <span>{{ t('sql.output_limit_scan_warning') }}</span>
          </div>

          <!-- Result tab -->
          <div v-show="(activeRuntime?.resultTab ?? 'result') === 'result'" class="flex-1 min-h-0 flex flex-col">
            <div
              v-if="resultPanelError"
              class="flex items-start gap-2 px-5 py-3 border-b chrome-border-subtle text-sm"
              style="background-color: rgb(239 68 68 / 0.08); color: rgb(185 28 28);"
            >
              <AlertTriangle class="w-4 h-4 shrink-0 mt-0.5" />
              <span class="font-mono whitespace-pre-wrap">{{ resultPanelError }}</span>
            </div>
            <div class="flex-1 min-h-0">
              <ResultTable
                v-if="shouldShowResultTable && activeRuntime?.result"
                :columns="activeRuntime.result.columns"
                :rows="activeRuntime.result.rows"
                :offset="activeRuntime.result.offset"
                :limit="activeRuntime.result.limit"
                :loaded-rows="activeRuntime.result.loaded_rows"
                :total-rows="activeRuntime.result.total_rows"
                :truncated="activeRuntime.result.truncated"
                :has-more="activeRuntime.result.has_more"
                :pagination-mode="activeRuntime.result.pagination_mode"
                :pagination-reason="activeRuntime.result.pagination_reason"
                :preview-truncated-cells="activeRuntime.result.preview_truncated_cells"
                :max-result-rows="activeRuntime.result.max_result_rows"
                :loading="activeRuntime.resultLoading"
                @change-page="onChangePage"
              />
              <div v-else class="h-full grid place-items-center chrome-text-muted text-sm">
                <div v-if="activeRuntime?.status && ACTIVE.has(activeRuntime.status)" class="flex items-center gap-2">
                  <LoadingDots />
                  <span>{{ statusSummary }}</span>
                </div>
                <span v-else>{{ t('sql.run_to_see_result') }}</span>
              </div>
            </div>
          </div>

          <!-- Plan tab -->
          <div v-show="activeRuntime?.resultTab === 'plan'" class="flex-1 min-h-0 flex flex-col">
            <!-- AI 慢 SQL 根因诊断入口(C4;与 EXPLAIN 计划展示同区,深诊断) -->
            <div class="flex items-center gap-2 px-5 py-2 border-b chrome-border-subtle">
              <button
                type="button"
                class="chrome-btn-secondary"
                :disabled="writesBlocked || aiDiagnoseBusy || !selectedDsId || !editorSql.trim()"
                :title="writesBlocked ? t('license.writes_blocked') : t('sql.ai_diagnose_hint')"
                @click="onDiagnoseSlowSql"
              >
                <Sparkles class="w-3.5 h-3.5" />
                {{ aiDiagnoseBusy ? t('sql.ai_diagnosing') : t('sql.ai_diagnose') }}
              </button>
              <span class="text-xs chrome-text-muted">{{ t('sql.ai_diagnose_hint') }}</span>
            </div>
            <div
              v-if="aiDiagnoseDisabled"
              class="flex items-center gap-2 px-5 py-2 border-b chrome-border-subtle text-xs"
              style="background-color: rgb(180 83 9 / 0.1); color: rgb(180 83 9)"
            >
              <AlertTriangle class="w-3.5 h-3.5 shrink-0" />
              <span>{{ t('sql.ai_disabled') }}</span>
            </div>
            <p
              v-if="aiDiagnoseError"
              class="px-5 py-2 border-b chrome-border-subtle text-xs text-red-600 dark:text-red-400"
            >
              {{ aiDiagnoseError }}
            </p>
            <div
              v-if="aiDiagnosis"
              class="px-5 py-3 border-b chrome-border-subtle text-sm chrome-bg-elevated"
            >
              <div class="flex items-center gap-1.5 mb-1 text-xs chrome-text-muted">
                <Sparkles class="w-3 h-3 shrink-0" />
                <span>{{ t('sql.ai_diagnose_generated') }}</span>
                <span v-if="aiDiagnoseBaseline && !aiDiagnoseBaseline.available">
                  · {{ t('sql.ai_diagnose_no_baseline') }}
                </span>
                <span v-else-if="aiDiagnoseBaseline">
                  · {{ t('sql.ai_diagnose_baseline_runs', { n: aiDiagnoseBaseline.runs }) }}
                </span>
              </div>
              <p class="whitespace-pre-wrap chrome-text-heading">{{ aiDiagnosis }}</p>
            </div>
            <div
              v-if="activeRuntime?.planError"
              class="flex items-start gap-2 px-5 py-3 border-b chrome-border-subtle text-sm"
              style="background-color: rgb(239 68 68 / 0.08); color: rgb(185 28 28);"
            >
              <AlertTriangle class="w-4 h-4 shrink-0 mt-0.5" />
              <span class="font-mono whitespace-pre-wrap">{{ activeRuntime.planError }}</span>
            </div>
            <div class="flex-1 min-h-0">
              <ResultTable
                v-if="activeRuntime?.planResult && activeRuntime.planResult.columns.length > 0"
                :columns="activeRuntime.planResult.columns"
                :rows="activeRuntime.planResult.rows"
                :offset="activeRuntime.planResult.offset"
                :limit="activeRuntime.planResult.limit"
                :loaded-rows="activeRuntime.planResult.loaded_rows"
                :total-rows="activeRuntime.planResult.total_rows"
                :truncated="activeRuntime.planResult.truncated"
              />
              <div v-else class="h-full grid place-items-center chrome-text-muted text-sm">
                <div v-if="planActive" class="flex items-center gap-2">
                  <LoadingDots />
                  <span>{{ t('sql.plan_running') }}</span>
                </div>
                <span v-else>{{ t('sql.plan_empty') }}</span>
              </div>
            </div>
          </div>

          <!-- Stats tab -->
          <div v-show="activeRuntime?.resultTab === 'stats'" class="flex-1 min-h-0 overflow-auto">
            <table v-if="activeRuntime?.status" class="w-full text-sm">
              <tbody>
                <tr v-for="row in statRows" :key="row.label" class="border-b chrome-border-subtle">
                  <td class="px-5 py-2 chrome-text-muted w-48">{{ row.label }}</td>
                  <td class="px-5 py-2 chrome-text-heading font-mono break-all">{{ row.value }}</td>
                </tr>
              </tbody>
            </table>
            <div v-else class="h-full grid place-items-center chrome-text-muted text-sm">
              {{ t('sql.run_to_see_result') }}
            </div>
          </div>
        </div>
      </template>
    </main>

    <AiSqlAssistantPanel
      :open="aiPanelOpen"
      :datasource-id="selectedDsId"
      :editor-sql="editorSql"
      @apply="applyGeneratedSql"
      @close="aiPanelOpen = false"
    />

    <Modal
      :open="templateModalOpen"
      :title="templateForm.id ? t('sql.template_edit') : t('sql.template_new')"
      @close="templateModalOpen = false"
    >
      <form class="space-y-3" @submit.prevent="submitTemplateForm">
        <label class="block">
          <span class="block text-xs chrome-text-muted mb-1">{{ t('sql.template_name') }}</span>
          <input v-model="templateForm.name" class="chrome-input w-full" required />
        </label>
        <label class="block">
          <span class="block text-xs chrome-text-muted mb-1">{{ t('sql.template_category') }}</span>
          <input v-model="templateForm.category" class="chrome-input w-full" required />
        </label>
        <label class="block">
          <span class="block text-xs chrome-text-muted mb-1">{{ t('sql.template_description') }}</span>
          <input v-model="templateForm.description" class="chrome-input w-full" />
        </label>
        <label class="block">
          <span class="block text-xs chrome-text-muted mb-1">{{ t('sql.template_variables') }}</span>
          <input v-model="templateForm.variables" class="chrome-input w-full font-mono" />
        </label>
        <label class="block">
          <span class="block text-xs chrome-text-muted mb-1">{{ t('sql.template_sql') }}</span>
          <textarea v-model="templateForm.sql_text" class="chrome-input w-full font-mono min-h-40" required />
        </label>
        <div class="flex justify-end gap-2 pt-2">
          <button type="button" class="chrome-btn-secondary" @click="templateModalOpen = false">
            <X class="w-3.5 h-3.5" />
            {{ t('common.cancel') }}
          </button>
          <button
            type="submit"
            class="chrome-btn-primary"
            :disabled="writesBlocked || templateSaving"
            :title="writesBlocked ? t('license.writes_blocked') : ''"
          >
            <Save class="w-3.5 h-3.5" />
            {{ templateSaving ? t('common.submitting') : t('common.save') }}
          </button>
        </div>
      </form>
    </Modal>

  </div>
</template>
