<script setup lang="ts">
/**
 * CompareView —— /projects/:id/compare(2.2.0 数据对比,PRD §12)
 *
 * 左:任务列表 + 任务建议;右:任务编辑(自动推断确认制)/ 4 桶结果 / 差异画像 / AI 归因。
 * compare_run 是异步 job,沿用 SQL workspace 的 job 轮询范式(轮 GET /jobs/{id} 到终态,
 * 终态后拉 /compare/runs/{run_id}/results|profile)。
 *
 * 字段全部锚 api/compare.ts(锚后端 schemas.py + 契约测试),不臆造。
 */
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useQuery } from '@tanstack/vue-query'
import {
  AlertTriangle,
  ArrowRight,
  Bot,
  Check,
  Download,
  GitCompareArrows,
  Play,
  Plus,
  RefreshCw,
  Save,
  Sparkles,
  Square,
  Trash2,
  Wand2,
  X,
} from 'lucide-vue-next'
import { listDatasources } from '../api/datasources'
import {
  COMPARE_BUCKETS,
  createCompareExport,
  createCompareTask,
  deleteCompareTask,
  downloadCompareExport,
  draftCompareTask,
  explainCompareRun,
  getCompareRunProfile,
  getCompareRunResults,
  inferCompareTask,
  listCompareTasks,
  runCompareTask,
  suggestCompareTasks,
  updateCompareTask,
  type ColumnMappingCandidate,
  type CompareAiAttributionResponse,
  type CompareBucket,
  type CompareInferResponse,
  type CompareResultRow,
  type CompareRunProfileResponse,
  type CompareRunResultResponse,
  type CompareTaskResponse,
  type PrimaryKeyCandidate,
  type TablePairSuggestion,
} from '../api/compare'
import { getJob, cancelJob, type JobResponse } from '../api/jobs'
import { ApiError, type DatasourceListItem, type JobStatus } from '../api/types'
import LoadingDots from '../components/LoadingDots.vue'

const TERMINAL: ReadonlySet<JobStatus> = new Set<JobStatus>([
  'success',
  'failed',
  'cancelled',
  'timeout',
])
const ACTIVE: ReadonlySet<JobStatus> = new Set<JobStatus>(['pending', 'running'])
const POLL_MS = 800
const PAGE_SIZE = 100

type RightTab = 'editor' | 'results' | 'profile'

interface BucketStyle {
  dot: string
  badge: string
  active: string
}
// 4 桶语义色硬绑(像 JobStatusBadge),与 variant 无关 —— PRD §12 视觉锚点 4 色 token。
const BUCKET_STYLE: Record<CompareBucket, BucketStyle> = {
  only_source: {
    dot: 'bg-red-500',
    badge: 'text-red-700 dark:text-red-300',
    active: 'border-red-400 bg-red-50 text-red-700 dark:bg-red-500/15 dark:text-red-300 dark:border-red-500/40',
  },
  only_target: {
    dot: 'bg-emerald-500',
    badge: 'text-emerald-700 dark:text-emerald-300',
    active:
      'border-emerald-400 bg-emerald-50 text-emerald-700 dark:bg-emerald-500/15 dark:text-emerald-300 dark:border-emerald-500/40',
  },
  diff: {
    dot: 'bg-amber-500',
    badge: 'text-amber-700 dark:text-amber-300',
    active:
      'border-amber-400 bg-amber-50 text-amber-700 dark:bg-amber-500/15 dark:text-amber-300 dark:border-amber-500/40',
  },
  same: {
    dot: 'bg-slate-400',
    badge: 'text-slate-600 dark:text-slate-300',
    active:
      'border-slate-400 bg-slate-100 text-slate-700 dark:bg-slate-500/15 dark:text-slate-300 dark:border-slate-500/40',
  },
}

const { t } = useI18n()
const route = useRoute()
const projectId = computed(() => (typeof route.params.id === 'string' ? route.params.id : ''))

// ── datasources ─────────────────────────────────────────────────────
const dsQuery = useQuery({
  queryKey: computed(() => ['datasources', projectId.value]),
  queryFn: () => listDatasources(projectId.value),
  enabled: computed(() => Boolean(projectId.value)),
})
const datasources = computed<DatasourceListItem[]>(() => dsQuery.data.value ?? [])

// ── tasks (left) ────────────────────────────────────────────────────
const tasks = ref<CompareTaskResponse[]>([])
const tasksLoading = ref(false)
const tasksError = ref<string | null>(null)
const activeTaskId = ref<string | null>(null)
const rightTab = ref<RightTab>('editor')

const activeTask = computed<CompareTaskResponse | null>(
  () => tasks.value.find((task) => task.id === activeTaskId.value) ?? null,
)

// ── editor draft (a working copy of the active task) ────────────────
interface EditorDraft {
  name: string
  sourceId: string
  targetId: string
  sourceSchema: string
  sourceTable: string
  targetSchema: string
  targetTable: string
  keyColumns: string[]
  columnMappings: Record<string, string>
  ignoreColumns: string[]
  columns: { name: string; type: string }[]
  numericTolerance: string
  trimStrings: boolean
  caseInsensitive: boolean
  emptyAsNull: boolean
  schemaPolicy: 'warn' | 'strict'
  recursiveChecksum: boolean
  bisectionFactor: number
  bisectionThreshold: number
  maxBisectionDepth: number
  sampleQuickCheck: boolean
  sampleSize: number
  sampleConfidence: number
  streamCompare: boolean
  persistSame: boolean
}

function emptyDraft(): EditorDraft {
  return {
    name: '',
    sourceId: '',
    targetId: '',
    sourceSchema: '',
    sourceTable: '',
    targetSchema: '',
    targetTable: '',
    keyColumns: [],
    columnMappings: {},
    ignoreColumns: [],
    columns: [],
    numericTolerance: '',
    trimStrings: false,
    caseInsensitive: false,
    emptyAsNull: false,
    schemaPolicy: 'warn',
    recursiveChecksum: true,
    bisectionFactor: 16,
    bisectionThreshold: 16000,
    maxBisectionDepth: 8,
    sampleQuickCheck: false,
    sampleSize: 300,
    sampleConfidence: 0.95,
    streamCompare: true,
    persistSame: false,
  }
}

const draft = reactive<EditorDraft>(emptyDraft())
const isNewTask = ref(false)
const savingTask = ref(false)
const editorError = ref<string | null>(null)

// ── inference draft ─────────────────────────────────────────────────
const inferResult = ref<CompareInferResponse | null>(null)
const inferring = ref(false)
const inferError = ref<string | null>(null)

// ── suggestions ─────────────────────────────────────────────────────
const suggestSourceId = ref('')
const suggestTargetId = ref('')
const suggestions = ref<TablePairSuggestion[]>([])
const suggesting = ref(false)
const suggestError = ref<string | null>(null)
const draftingPair = ref<string | null>(null)

// ── run / results ───────────────────────────────────────────────────
interface RunState {
  jobId: string | null
  runId: string | null
  status: JobStatus | null
  error: string | null
  cancelling: boolean
}
const run = reactive<RunState>({ jobId: null, runId: null, status: null, error: null, cancelling: false })
let pollTimer: ReturnType<typeof setTimeout> | null = null
let exportPollTimer: ReturnType<typeof setTimeout> | null = null

const resultBucket = ref<CompareBucket>('diff')
const resultData = ref<CompareRunResultResponse | null>(null)
const resultLoading = ref(false)
const resultError = ref<string | null>(null)

const profileData = ref<CompareRunProfileResponse | null>(null)
const profileLoading = ref(false)
const profileError = ref<string | null>(null)

// ── AI attribution ──────────────────────────────────────────────────
const aiResult = ref<CompareAiAttributionResponse | null>(null)
const aiBusy = ref(false)

const runActive = computed<boolean>(() => (run.status ? ACTIVE.has(run.status) : false))
const exportBusy = ref(false)
const exportError = ref<string | null>(null)
const exportReady = ref<{ token: string; filename: string } | null>(null)
const exportableRunId = computed<string | null>(() =>
  run.runId && run.status === 'success' ? run.runId : null,
)

const bucketCounts = computed<Record<CompareBucket, number>>(
  () =>
    resultData.value?.bucket_counts ??
    profileData.value?.bucket_counts ?? { only_source: 0, only_target: 0, diff: 0, same: 0 },
)
const progress = computed(() => resultData.value?.progress ?? profileData.value?.progress ?? {})
const sampleResult = computed(
  () => resultData.value?.sample_result ?? profileData.value?.sample_result ?? null,
)
const diffProfile = computed(
  () => resultData.value?.diff_profile ?? profileData.value?.diff_profile ?? {},
)

// 主键列固定左侧:diff tab 表头依推断/规则的 key_columns 决定。
const keyColumns = computed<string[]>(() => draft.keyColumns)
const compareColumns = computed<string[]>(() =>
  draft.columns.map((c) => c.name).filter((name) => !draft.ignoreColumns.includes(name)),
)

const runBlockedReason = computed<string | null>(() => {
  if (draft.keyColumns.length === 0 && !draft.sampleQuickCheck) return t('compare.run_blocked_no_pk')
  if (compareColumns.value.length === 0) return t('compare.run_blocked_no_columns')
  return null
})

// ── lifecycle ───────────────────────────────────────────────────────
onMounted(() => {
  void loadTasks()
})
onUnmounted(() => {
  if (pollTimer) clearTimeout(pollTimer)
  if (exportPollTimer) clearTimeout(exportPollTimer)
})

watch(activeTask, (task) => {
  if (task) {
    loadDraftFromTask(task)
    isNewTask.value = false
    resetRunState()
    rightTab.value = 'editor'
  }
})

watch(datasources, (list) => {
  if (!draft.sourceId && list.length > 0) draft.sourceId = list[0].id
  if (!draft.targetId && list.length > 0) draft.targetId = list[0].id
  if (!suggestSourceId.value && list.length > 0) suggestSourceId.value = list[0].id
  if (!suggestTargetId.value && list.length > 0) suggestTargetId.value = list[0].id
})

// ── tasks loading ───────────────────────────────────────────────────
async function loadTasks(): Promise<void> {
  if (!projectId.value) return
  tasksLoading.value = true
  tasksError.value = null
  try {
    tasks.value = await listCompareTasks(projectId.value)
    if (!activeTaskId.value && tasks.value.length > 0) activeTaskId.value = tasks.value[0].id
  } catch (e) {
    tasksError.value = errorMessage(e)
  } finally {
    tasksLoading.value = false
  }
}

function loadDraftFromTask(task: CompareTaskResponse): void {
  Object.assign(draft, emptyDraft())
  draft.name = task.name
  draft.sourceId = task.source_id
  draft.targetId = task.target_id
  draft.sourceSchema = task.source_ref.schema_name ?? ''
  draft.sourceTable = task.source_ref.table_name ?? ''
  draft.targetSchema = task.target_ref.schema_name ?? ''
  draft.targetTable = task.target_ref.table_name ?? ''
  draft.columns = task.columns.map((c) => ({ name: c.name, type: c.type }))
  draft.keyColumns = [...task.compare_rules.key_columns]
  draft.columnMappings = { ...task.compare_rules.column_mappings }
  draft.ignoreColumns = [...task.compare_rules.ignore_columns]
  draft.numericTolerance =
    task.compare_rules.numeric_tolerance != null ? String(task.compare_rules.numeric_tolerance) : ''
  draft.trimStrings = task.compare_rules.trim_strings
  draft.caseInsensitive = task.compare_rules.case_insensitive
  draft.emptyAsNull = task.compare_rules.empty_as_null
  draft.schemaPolicy = task.compare_rules.schema_policy
  draft.recursiveChecksum = task.run_limits.recursive_checksum
  draft.bisectionFactor = task.run_limits.bisection_factor
  draft.bisectionThreshold = task.run_limits.bisection_threshold
  draft.maxBisectionDepth = task.run_limits.max_bisection_depth
  draft.sampleQuickCheck = task.run_limits.sample_quick_check
  draft.sampleSize = task.run_limits.sample_size
  draft.sampleConfidence = task.run_limits.sample_confidence
  draft.streamCompare = task.run_limits.stream_compare
  draft.persistSame = task.run_limits.persist_same_bucket
  inferResult.value = null
}

function startNewTask(): void {
  activeTaskId.value = null
  isNewTask.value = true
  Object.assign(draft, emptyDraft())
  draft.sourceId = datasources.value[0]?.id ?? ''
  draft.targetId = datasources.value[0]?.id ?? ''
  draft.name = ''
  inferResult.value = null
  inferError.value = null
  resetRunState()
  rightTab.value = 'editor'
}

// ── inference ───────────────────────────────────────────────────────
async function onInfer(): Promise<void> {
  if (!draft.sourceId || !draft.targetId || !draft.sourceTable || !draft.targetTable) {
    inferError.value = t('compare.infer_hint')
    return
  }
  inferError.value = null
  inferring.value = true
  try {
    const res = await inferCompareTask(projectId.value, {
      source_id: draft.sourceId,
      target_id: draft.targetId,
      source_table: { schema_name: draft.sourceSchema, table_name: draft.sourceTable },
      target_table: { schema_name: draft.targetSchema, table_name: draft.targetTable },
    })
    inferResult.value = res
    // 把推断的规则草稿落进 draft —— 用户在 UI 里确认/调整/忽略,不是空白手填。
    draft.columns = res.columns.map((c) => ({ name: c.name, type: c.type }))
    draft.keyColumns = [...res.compare_rules.key_columns]
    draft.columnMappings = { ...res.compare_rules.column_mappings }
    draft.ignoreColumns = [...res.compare_rules.ignore_columns]
  } catch (e) {
    inferError.value = errorMessage(e)
  } finally {
    inferring.value = false
  }
}

function applyPkCandidate(candidate: PrimaryKeyCandidate): void {
  draft.keyColumns = [...candidate.source_columns]
  // 映射主键列 source→target(若不同名)
  candidate.source_columns.forEach((srcCol, idx) => {
    const tgtCol = candidate.target_columns[idx]
    if (tgtCol && tgtCol !== srcCol) draft.columnMappings[srcCol] = tgtCol
  })
}

function toggleIgnoreColumn(name: string): void {
  if (draft.ignoreColumns.includes(name)) {
    draft.ignoreColumns = draft.ignoreColumns.filter((c) => c !== name)
  } else {
    draft.ignoreColumns = [...draft.ignoreColumns, name]
  }
}

function applyMapping(mapping: ColumnMappingCandidate): void {
  draft.columnMappings[mapping.source_column] = mapping.target_column
}

// ── save ────────────────────────────────────────────────────────────
function buildRulesPayload() {
  return {
    key_columns: draft.keyColumns,
    ignore_columns: draft.ignoreColumns,
    column_mappings: draft.columnMappings,
    numeric_tolerance: draft.numericTolerance.trim() ? Number(draft.numericTolerance) : null,
    trim_strings: draft.trimStrings,
    case_insensitive: draft.caseInsensitive,
    empty_as_null: draft.emptyAsNull,
    schema_policy: draft.schemaPolicy,
  }
}
function buildLimitsPayload() {
  return {
    recursive_checksum: draft.recursiveChecksum,
    bisection_factor: draft.bisectionFactor,
    bisection_threshold: draft.bisectionThreshold,
    max_bisection_depth: draft.maxBisectionDepth,
    sample_quick_check: draft.sampleQuickCheck,
    sample_size: draft.sampleSize,
    sample_confidence: draft.sampleConfidence,
    stream_compare: draft.streamCompare,
    persist_same_bucket: draft.persistSame,
  }
}

async function onSaveTask(): Promise<void> {
  if (!draft.name.trim() || !draft.sourceTable || !draft.targetTable) {
    editorError.value = t('compare.infer_hint')
    return
  }
  editorError.value = null
  savingTask.value = true
  const sourceRef = { kind: 'table' as const, schema_name: draft.sourceSchema || null, table_name: draft.sourceTable }
  const targetRef = { kind: 'table' as const, schema_name: draft.targetSchema || null, table_name: draft.targetTable }
  try {
    if (isNewTask.value || !activeTask.value) {
      const created = await createCompareTask({
        project_id: projectId.value,
        name: draft.name.trim(),
        source_id: draft.sourceId,
        target_id: draft.targetId,
        source_ref: sourceRef,
        target_ref: targetRef,
        columns: draft.columns.map((c) => ({ name: c.name, type: c.type as never, driver_type: null, nullable: true, primary_key: draft.keyColumns.includes(c.name) })),
        compare_rules: buildRulesPayload(),
        run_limits: buildLimitsPayload(),
      })
      tasks.value = [created, ...tasks.value]
      activeTaskId.value = created.id
      isNewTask.value = false
    } else {
      const updated = await updateCompareTask(activeTask.value.id, {
        name: draft.name.trim(),
        source_id: draft.sourceId,
        target_id: draft.targetId,
        source_ref: sourceRef,
        target_ref: targetRef,
        columns: draft.columns.map((c) => ({ name: c.name, type: c.type as never, driver_type: null, nullable: true, primary_key: draft.keyColumns.includes(c.name) })),
        compare_rules: buildRulesPayload(),
        run_limits: buildLimitsPayload(),
      })
      mergeTask(updated)
    }
  } catch (e) {
    editorError.value = errorMessage(e)
  } finally {
    savingTask.value = false
  }
}

function mergeTask(task: CompareTaskResponse): void {
  const idx = tasks.value.findIndex((item) => item.id === task.id)
  if (idx === -1) tasks.value = [task, ...tasks.value]
  else tasks.value.splice(idx, 1, task)
}

async function onDeleteTask(): Promise<void> {
  if (!activeTask.value) return
  const id = activeTask.value.id
  try {
    await deleteCompareTask(id)
    tasks.value = tasks.value.filter((task) => task.id !== id)
    activeTaskId.value = tasks.value[0]?.id ?? null
    if (!activeTaskId.value) startNewTask()
  } catch (e) {
    editorError.value = errorMessage(e)
  }
}

// ── suggestions ─────────────────────────────────────────────────────
async function onLoadSuggestions(): Promise<void> {
  if (!suggestSourceId.value || !suggestTargetId.value) return
  suggestError.value = null
  suggesting.value = true
  try {
    const res = await suggestCompareTasks(projectId.value, suggestSourceId.value, suggestTargetId.value)
    suggestions.value = res.suggestions
  } catch (e) {
    suggestError.value = errorMessage(e)
  } finally {
    suggesting.value = false
  }
}

async function onCreateDraftFromPair(pair: TablePairSuggestion): Promise<void> {
  const key = `${pair.source_schema}.${pair.source_table}`
  draftingPair.value = key
  suggestError.value = null
  try {
    const created = await draftCompareTask(projectId.value, {
      source_id: suggestSourceId.value,
      target_id: suggestTargetId.value,
      source_table: { schema_name: pair.source_schema, table_name: pair.source_table },
      target_table: { schema_name: pair.target_schema, table_name: pair.target_table },
    })
    tasks.value = [created, ...tasks.value]
    activeTaskId.value = created.id
  } catch (e) {
    // needs_manual_pk → 409;提示用户走任务编辑手动选主键。
    if (e instanceof ApiError && e.code === 'needs_manual_pk') {
      suggestError.value = t('compare.pk_needs_manual')
    } else {
      suggestError.value = errorMessage(e)
    }
  } finally {
    draftingPair.value = null
  }
}

// ── run + poll ──────────────────────────────────────────────────────
function resetExportState(): void {
  if (exportPollTimer) clearTimeout(exportPollTimer)
  exportPollTimer = null
  exportBusy.value = false
  exportError.value = null
  exportReady.value = null
}

function resetRunState(): void {
  if (pollTimer) clearTimeout(pollTimer)
  pollTimer = null
  run.jobId = null
  run.runId = null
  run.status = null
  run.error = null
  run.cancelling = false
  resultData.value = null
  profileData.value = null
  aiResult.value = null
  resetExportState()
}

async function onRun(): Promise<void> {
  if (!activeTask.value) {
    // 必须先保存才能跑(run 端点按 task_id)
    await onSaveTask()
  }
  if (!activeTask.value || runBlockedReason.value) return
  resetRunState()
  run.status = 'pending'
  rightTab.value = 'results'
  try {
    const res = await runCompareTask(activeTask.value.id)
    run.jobId = res.job_id
    run.runId = res.run_id
    startPoll()
  } catch (e) {
    run.status = null
    run.error = errorMessage(e)
  }
}

function startPoll(): void {
  void pollRun()
}

async function pollRun(): Promise<void> {
  if (!run.jobId) return
  try {
    const job: JobResponse = await getJob(run.jobId)
    run.status = job.status
    if (job.status === 'failed' || job.status === 'timeout') run.error = job.error
    if (job.status === 'cancelled') run.cancelling = false
    if (TERMINAL.has(job.status)) {
      if (job.status === 'success') {
        await Promise.all([loadResults(resultBucket.value), loadProfile()])
      }
      return
    }
  } catch (e) {
    run.error = errorMessage(e)
    return
  }
  pollTimer = setTimeout(() => {
    void pollRun()
  }, POLL_MS)
}

async function onCancelRun(): Promise<void> {
  if (!run.jobId || run.cancelling) return
  run.cancelling = true
  try {
    await cancelJob(run.jobId)
  } catch (e) {
    run.cancelling = false
    run.error = errorMessage(e)
  }
}

async function onCreateCompareExport(): Promise<void> {
  const runId = exportableRunId.value
  if (!runId || exportBusy.value) return
  resetExportState()
  exportBusy.value = true
  try {
    const res = await createCompareExport(runId)
    await pollCompareExportJob(res.job_id, res.download_token, res.filename)
  } catch (e) {
    exportBusy.value = false
    exportError.value = compareExportErrorMessage(e)
  }
}

function compareExportErrorMessage(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 429) return t('compare.export_rate_limited')
    if (e.status === 404 || e.status === 409) return t('compare.export_source_gone')
    if (e.status === 410) return t('compare.export_token_spent')
  }
  return errorMessage(e)
}

async function pollCompareExportJob(
  jobId: string,
  token: string,
  filename: string,
): Promise<void> {
  try {
    const job = await getJob(jobId)
    if (TERMINAL.has(job.status)) {
      exportPollTimer = null
      exportBusy.value = false
      if (job.status === 'success') {
        exportReady.value = { token, filename }
      } else {
        exportError.value = job.error || t('compare.export_failed')
      }
      return
    }
  } catch (e) {
    exportPollTimer = null
    exportBusy.value = false
    exportError.value = errorMessage(e)
    return
  }
  exportPollTimer = setTimeout(() => {
    void pollCompareExportJob(jobId, token, filename)
  }, POLL_MS)
}

async function onDownloadCompareExport(): Promise<void> {
  if (!exportReady.value) return
  const ready = exportReady.value
  try {
    await downloadCompareExport(ready.token, ready.filename)
    exportReady.value = null
  } catch (e) {
    exportError.value = compareExportErrorMessage(e)
    exportReady.value = null
  }
}

// ── results / profile ───────────────────────────────────────────────
async function loadResults(bucket: CompareBucket): Promise<void> {
  if (!run.runId) return
  resultBucket.value = bucket
  resultLoading.value = true
  resultError.value = null
  try {
    resultData.value = await getCompareRunResults(run.runId, bucket, 0, PAGE_SIZE)
  } catch (e) {
    resultError.value = errorMessage(e)
  } finally {
    resultLoading.value = false
  }
}

async function onSelectBucket(bucket: CompareBucket): Promise<void> {
  await loadResults(bucket)
}

async function loadProfile(): Promise<void> {
  if (!run.runId) return
  profileLoading.value = true
  profileError.value = null
  try {
    profileData.value = await getCompareRunProfile(run.runId)
  } catch (e) {
    profileError.value = errorMessage(e)
  } finally {
    profileLoading.value = false
  }
}

async function onAiExplain(): Promise<void> {
  if (!run.runId || aiBusy.value) return
  aiBusy.value = true
  try {
    aiResult.value = await explainCompareRun(run.runId)
  } catch (e) {
    aiResult.value = {
      run_id: run.runId,
      ok: false,
      attribution: null,
      provider: null,
      model: null,
      error: errorMessage(e),
      egress_level: 2,
    }
  } finally {
    aiBusy.value = false
  }
}

// ── helpers ─────────────────────────────────────────────────────────
function datasourceName(id: string): string {
  return datasources.value.find((d) => d.id === id)?.name ?? id.slice(0, 8)
}

function fmtCell(value: unknown): string {
  if (value === null || value === undefined) return '∅'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

function cellDiffFor(row: CompareResultRow, column: string) {
  return row.cells.find((c) => c.column === column)
}

function diffRatePct(rate: number | undefined): number {
  return Math.round((rate ?? 0) * 100)
}

function formatDate(iso: string | null): string {
  if (!iso) return t('compare.never_run')
  try {
    return new Date(iso).toLocaleString()
  } catch {
    return iso
  }
}

function errorMessage(e: unknown): string {
  if (e instanceof ApiError) return e.message || t('common.error_unknown')
  return t('common.error_unknown')
}

const profileColumns = computed(() => {
  const cols = diffProfile.value.columns ?? {}
  return Object.entries(cols).map(([name, info]) => ({ name, info }))
})
const missingSource = computed(
  () => (diffProfile.value.missing_key_ranges?.only_source ?? []) as unknown[],
)
const missingTarget = computed(
  () => (diffProfile.value.missing_key_ranges?.only_target ?? []) as unknown[],
)
</script>

<template>
  <div class="flex h-full min-h-0 chrome-bg-main">
    <!-- ── 左:任务列表 + 建议 ───────────────────────────────────── -->
    <aside class="w-[20rem] shrink-0 border-r chrome-border chrome-bg-panel flex flex-col min-h-0">
      <div class="px-4 py-3 border-b chrome-border-subtle">
        <div class="text-xs uppercase tracking-wider chrome-text-muted font-medium">
          {{ t('nav.projects') }} / {{ projectId.slice(0, 8) }}
        </div>
        <div class="text-section font-semibold chrome-text-heading mt-1 flex items-center gap-2">
          <GitCompareArrows class="w-4 h-4" />
          {{ t('compare.workspace') }}
        </div>
      </div>

      <div class="flex items-center gap-2 px-3 py-2 border-b chrome-border-subtle">
        <span class="text-xs font-medium uppercase tracking-wider chrome-text-muted flex-1">
          {{ t('compare.tasks') }}
        </span>
        <button type="button" class="chrome-btn-ghost" :title="t('compare.new_task')" @click="startNewTask">
          <Plus class="w-4 h-4" />
        </button>
      </div>

      <div v-if="tasksError" class="px-3 py-2 text-xs text-red-600 dark:text-red-400">{{ tasksError }}</div>
      <div v-if="tasksLoading" class="p-4 chrome-text-muted text-sm"><LoadingDots /></div>
      <div v-else class="overflow-auto p-2 space-y-1" style="max-height: 45%">
        <div v-if="tasks.length === 0" class="p-3 text-sm chrome-text-muted">
          {{ t('compare.task_empty') }}
        </div>
        <button
          v-for="task in tasks"
          :key="task.id"
          type="button"
          class="w-full text-left rounded-card border px-3 py-2 transition-colors"
          :class="
            activeTaskId === task.id
              ? 'chrome-border chrome-accent-light-bg'
              : 'border-transparent hover:chrome-bg-elevated'
          "
          @click="activeTaskId = task.id"
        >
          <div class="flex items-center gap-2 min-w-0">
            <span class="w-2 h-2 rounded-full bg-slate-400 shrink-0" />
            <span class="flex-1 truncate text-sm chrome-text-heading">{{ task.name }}</span>
          </div>
          <div class="mt-1 text-[11px] chrome-text-muted truncate">
            {{ datasourceName(task.source_id) }} <ArrowRight class="inline w-3 h-3" />
            {{ datasourceName(task.target_id) }}
          </div>
          <div class="text-[11px] chrome-text-muted">
            {{ t('compare.last_run') }}: {{ formatDate(task.updated_at) }}
          </div>
        </button>
      </div>

      <!-- 任务建议 -->
      <div class="border-t chrome-border-subtle flex-1 min-h-0 flex flex-col">
        <div class="px-3 py-2 text-xs font-medium uppercase tracking-wider chrome-text-muted">
          {{ t('compare.suggestions') }}
        </div>
        <div class="px-3 pb-2 space-y-2">
          <p class="text-[11px] chrome-text-muted">{{ t('compare.suggestions_hint') }}</p>
          <select v-model="suggestSourceId" class="chrome-input w-full text-xs">
            <option v-for="ds in datasources" :key="ds.id" :value="ds.id">{{ ds.name }}</option>
          </select>
          <select v-model="suggestTargetId" class="chrome-input w-full text-xs">
            <option v-for="ds in datasources" :key="ds.id" :value="ds.id">{{ ds.name }}</option>
          </select>
          <button
            type="button"
            class="chrome-btn-secondary w-full justify-center text-xs"
            :disabled="suggesting || !suggestSourceId || !suggestTargetId"
            @click="onLoadSuggestions"
          >
            <RefreshCw class="w-3.5 h-3.5" :class="suggesting && 'animate-spin'" />
            {{ suggesting ? t('compare.suggesting') : t('compare.suggest_load') }}
          </button>
          <div v-if="suggestError" class="text-xs text-red-600 dark:text-red-400">{{ suggestError }}</div>
        </div>
        <div class="flex-1 min-h-0 overflow-auto px-2 pb-2 space-y-1">
          <div
            v-if="suggestions.length === 0 && !suggesting"
            class="px-3 py-2 text-xs chrome-text-muted"
          >
            {{ t('compare.suggestions_empty') }}
          </div>
          <div
            v-for="pair in suggestions"
            :key="`${pair.source_schema}.${pair.source_table}->${pair.target_table}`"
            class="rounded-card px-2 py-1.5 hover:chrome-bg-elevated flex items-center gap-2"
          >
            <div class="flex-1 min-w-0">
              <div class="text-xs chrome-text-heading truncate">
                {{ pair.source_table }} <ArrowRight class="inline w-3 h-3" /> {{ pair.target_table }}
              </div>
              <div class="text-[10px] chrome-text-muted">
                {{ t(`compare.reason_${pair.reason}`) }}
              </div>
            </div>
            <button
              type="button"
              class="chrome-btn-ghost"
              :title="t('compare.create_draft')"
              :disabled="draftingPair === `${pair.source_schema}.${pair.source_table}`"
              @click="onCreateDraftFromPair(pair)"
            >
              <Plus class="w-3.5 h-3.5" />
            </button>
          </div>
        </div>
      </div>
    </aside>

    <!-- ── 右:编辑 / 结果 / 画像 ─────────────────────────────────── -->
    <section class="flex-1 min-h-0 flex flex-col">
      <!-- 顶部 tab(.chrome-tab,图标+文字)-->
      <div class="flex items-center gap-1 px-3 py-2 border-b chrome-border-subtle">
        <button
          type="button"
          class="chrome-tab"
          :class="rightTab === 'editor' && 'chrome-accent-light-bg chrome-accent'"
          @click="rightTab = 'editor'"
        >
          <Wand2 class="w-4 h-4" /> {{ activeTask ? activeTask.name : t('compare.new_task') }}
        </button>
        <button
          type="button"
          class="chrome-tab"
          :class="rightTab === 'results' && 'chrome-accent-light-bg chrome-accent'"
          :disabled="!run.runId"
          @click="rightTab = 'results'"
        >
          <GitCompareArrows class="w-4 h-4" /> {{ t('compare.results') }}
        </button>
        <button
          type="button"
          class="chrome-tab"
          :class="rightTab === 'profile' && 'chrome-accent-light-bg chrome-accent'"
          :disabled="!run.runId"
          @click="rightTab = 'profile'"
        >
          <Sparkles class="w-4 h-4" /> {{ t('compare.profile') }}
        </button>

        <div class="flex-1" />

        <button
          type="button"
          class="chrome-btn-secondary text-xs"
          :disabled="!exportableRunId || exportBusy"
          :title="!exportableRunId ? t('compare.export_needs_success') : t('compare.export')"
          @click="onCreateCompareExport"
        >
          <Download class="w-3.5 h-3.5" :class="exportBusy && 'animate-pulse'" />
          {{ exportBusy ? t('compare.export_running') : t('compare.export') }}
        </button>

        <button
          v-if="runActive"
          type="button"
          class="chrome-btn-secondary text-xs"
          :disabled="run.cancelling"
          @click="onCancelRun"
        >
          <Square class="w-3.5 h-3.5" /> {{ t('compare.cancel_run') }}
        </button>
        <button
          type="button"
          class="chrome-btn-primary text-xs"
          :disabled="runActive || Boolean(runBlockedReason)"
          :title="runBlockedReason ?? ''"
          @click="onRun"
        >
          <Play class="w-3.5 h-3.5" /> {{ runActive ? t('compare.running') : t('compare.run') }}
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
        <span class="flex-1 chrome-text-heading">{{ t('compare.export_ready', { file: exportReady.filename }) }}</span>
        <button type="button" class="chrome-btn-secondary text-xs" @click="onDownloadCompareExport">
          {{ t('compare.export_download') }}
        </button>
      </div>

      <div class="flex-1 min-h-0 overflow-auto">
        <!-- ============ 编辑 tab ============ -->
        <div v-show="rightTab === 'editor'" class="p-4 space-y-4 max-w-4xl">
          <div v-if="runBlockedReason" class="flex items-center gap-2 text-xs text-amber-700 dark:text-amber-300">
            <AlertTriangle class="w-4 h-4" /> {{ runBlockedReason }}
          </div>

          <!-- 名称 + 源/目标 -->
          <div class="grid grid-cols-2 gap-4">
            <label class="block col-span-2">
              <span class="block text-xs chrome-text-muted mb-1">{{ t('compare.task_name') }}</span>
              <input v-model="draft.name" class="chrome-input w-full" />
            </label>
            <fieldset class="space-y-2 rounded-card border chrome-border p-3">
              <legend class="px-1 text-xs font-medium chrome-text-heading">{{ t('compare.source') }}</legend>
              <select v-model="draft.sourceId" class="chrome-input w-full text-sm">
                <option v-for="ds in datasources" :key="ds.id" :value="ds.id">{{ ds.name }}</option>
              </select>
              <input v-model="draft.sourceSchema" class="chrome-input w-full text-sm" :placeholder="t('compare.schema')" />
              <input v-model="draft.sourceTable" class="chrome-input w-full text-sm" :placeholder="t('compare.table')" />
            </fieldset>
            <fieldset class="space-y-2 rounded-card border chrome-border p-3">
              <legend class="px-1 text-xs font-medium chrome-text-heading">{{ t('compare.target') }}</legend>
              <select v-model="draft.targetId" class="chrome-input w-full text-sm">
                <option v-for="ds in datasources" :key="ds.id" :value="ds.id">{{ ds.name }}</option>
              </select>
              <input v-model="draft.targetSchema" class="chrome-input w-full text-sm" :placeholder="t('compare.schema')" />
              <input v-model="draft.targetTable" class="chrome-input w-full text-sm" :placeholder="t('compare.table')" />
            </fieldset>
          </div>

          <div class="flex items-center gap-2">
            <button type="button" class="chrome-btn-secondary text-sm" :disabled="inferring" @click="onInfer">
              <Wand2 class="w-4 h-4" :class="inferring && 'animate-pulse'" />
              {{ inferring ? t('compare.inferring') : t('compare.infer') }}
            </button>
            <span class="text-xs chrome-text-muted">{{ t('compare.infer_hint') }}</span>
          </div>
          <div v-if="inferError" class="text-xs text-red-600 dark:text-red-400">{{ inferError }}</div>

          <!-- 推断草稿:主键候选 -->
          <div v-if="inferResult" class="space-y-4">
            <div
              v-if="inferResult.needs_manual_pk"
              class="flex items-start gap-2 rounded-card border border-amber-300 dark:border-amber-500/40 bg-amber-50 dark:bg-amber-500/10 p-3 text-xs text-amber-800 dark:text-amber-200"
            >
              <AlertTriangle class="w-4 h-4 mt-0.5 shrink-0" /> {{ t('compare.pk_needs_manual') }}
            </div>

            <div>
              <div class="text-xs font-medium chrome-text-heading mb-2">{{ t('compare.pk_candidates') }}</div>
              <div v-if="inferResult.pk_candidates.length === 0" class="text-xs chrome-text-muted">
                {{ t('compare.no_pk_candidates') }}
              </div>
              <div
                v-for="(pk, idx) in inferResult.pk_candidates"
                :key="idx"
                class="flex items-center gap-2 rounded-card border chrome-border px-3 py-2 mb-1"
              >
                <span class="flex-1 text-sm chrome-text-heading font-mono">
                  {{ pk.source_columns.join(', ') }}
                  <ArrowRight class="inline w-3 h-3 chrome-text-muted" />
                  {{ pk.target_columns.join(', ') }}
                </span>
                <span class="inline-flex items-center rounded-input px-1.5 py-0.5 text-[10px] font-medium bg-sky-50 text-sky-700 dark:bg-sky-500/15 dark:text-sky-300">
                  {{ t(`compare.reason_${pk.reason}`) }}
                </span>
                <span class="text-[10px] chrome-text-muted">{{ Math.round(pk.confidence * 100) }}%</span>
                <button
                  type="button"
                  class="chrome-btn-ghost"
                  :title="t('compare.use_this_pk')"
                  @click="applyPkCandidate(pk)"
                >
                  <Check class="w-3.5 h-3.5" :class="JSON.stringify(draft.keyColumns) === JSON.stringify(pk.source_columns) && 'chrome-accent'" />
                </button>
              </div>
              <div v-if="draft.keyColumns.length > 0" class="mt-1 text-[11px] chrome-text-muted">
                {{ t('compare.pk_confirmed') }}: <span class="font-mono">{{ draft.keyColumns.join(', ') }}</span>
              </div>
            </div>

            <!-- 列映射候选 -->
            <div>
              <div class="text-xs font-medium chrome-text-heading mb-2">{{ t('compare.column_mappings') }}</div>
              <div v-if="inferResult.mappings.length === 0" class="text-xs chrome-text-muted">
                {{ t('compare.no_mappings') }}
              </div>
              <table v-else class="w-full text-xs border chrome-border rounded-card overflow-hidden">
                <thead class="chrome-bg-elevated">
                  <tr class="text-left chrome-text-muted">
                    <th class="px-2 py-1.5 font-medium">{{ t('compare.source_col') }}</th>
                    <th class="px-2 py-1.5 font-medium">{{ t('compare.target_col') }}</th>
                    <th class="px-2 py-1.5 font-medium">{{ t('compare.reason_exact') }}</th>
                    <th class="px-2 py-1.5 font-medium">{{ t('compare.confidence') }}</th>
                    <th class="px-2 py-1.5"></th>
                  </tr>
                </thead>
                <tbody>
                  <tr
                    v-for="m in inferResult.mappings"
                    :key="m.source_column"
                    class="border-t chrome-border-subtle"
                    :class="m.conflict && 'bg-amber-50 dark:bg-amber-500/10'"
                  >
                    <td class="px-2 py-1.5 font-mono chrome-text-heading">{{ m.source_column }}</td>
                    <td class="px-2 py-1.5 font-mono chrome-text-heading">
                      {{ draft.columnMappings[m.source_column] ?? m.target_column }}
                    </td>
                    <td class="px-2 py-1.5">
                      <span class="inline-flex items-center rounded-input px-1.5 py-0.5 text-[10px] font-medium bg-slate-100 text-slate-600 dark:bg-slate-500/15 dark:text-slate-300">
                        {{ t(`compare.reason_${m.reason}`) }}
                      </span>
                      <span v-if="m.conflict" class="ml-1 text-[10px] text-amber-700 dark:text-amber-300">
                        {{ t('compare.conflict') }}{{ m.conflict_kind ? ` (${m.conflict_kind})` : '' }}
                      </span>
                    </td>
                    <td class="px-2 py-1.5 chrome-text-muted">{{ Math.round(m.confidence * 100) }}%</td>
                    <td class="px-2 py-1.5">
                      <button type="button" class="chrome-btn-ghost" @click="applyMapping(m)">
                        <Check class="w-3.5 h-3.5" />
                      </button>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>

          <!-- 比较列选择(勾掉 ignore) -->
          <div v-if="draft.columns.length > 0">
            <div class="text-xs font-medium chrome-text-heading mb-2">{{ t('compare.compare_columns') }}</div>
            <div class="flex flex-wrap gap-1.5">
              <button
                v-for="col in draft.columns"
                :key="col.name"
                type="button"
                class="inline-flex items-center gap-1 rounded-input border px-2 py-1 text-xs"
                :class="
                  draft.ignoreColumns.includes(col.name)
                    ? 'border-dashed chrome-border chrome-text-muted line-through'
                    : 'chrome-border chrome-text-heading'
                "
                :title="t('compare.toggle_ignore')"
                @click="toggleIgnoreColumn(col.name)"
              >
                {{ col.name }}
                <span class="text-[10px] chrome-text-muted">{{ col.type }}</span>
              </button>
            </div>
          </div>

          <!-- 规则 + 限制 -->
          <div class="grid grid-cols-2 gap-4">
            <fieldset class="rounded-card border chrome-border p-3 space-y-2">
              <legend class="px-1 text-xs font-medium chrome-text-heading">{{ t('compare.rules') }}</legend>
              <label class="flex items-center justify-between text-xs">
                <span class="chrome-text-muted">{{ t('compare.rule_numeric_tolerance') }}</span>
                <input v-model="draft.numericTolerance" class="chrome-input w-24 text-xs" placeholder="0" />
              </label>
              <label class="flex items-center gap-2 text-xs chrome-text-muted">
                <input v-model="draft.trimStrings" type="checkbox" /> {{ t('compare.rule_trim_strings') }}
              </label>
              <label class="flex items-center gap-2 text-xs chrome-text-muted">
                <input v-model="draft.caseInsensitive" type="checkbox" /> {{ t('compare.rule_case_insensitive') }}
              </label>
              <label class="flex items-center gap-2 text-xs chrome-text-muted">
                <input v-model="draft.emptyAsNull" type="checkbox" /> {{ t('compare.rule_empty_as_null') }}
              </label>
              <label class="flex items-center justify-between text-xs">
                <span class="chrome-text-muted">{{ t('compare.rule_schema_policy') }}</span>
                <select v-model="draft.schemaPolicy" class="chrome-input text-xs">
                  <option value="warn">{{ t('compare.rule_policy_warn') }}</option>
                  <option value="strict">{{ t('compare.rule_policy_strict') }}</option>
                </select>
              </label>
            </fieldset>

            <fieldset class="rounded-card border chrome-border p-3 space-y-2">
              <legend class="px-1 text-xs font-medium chrome-text-heading">{{ t('compare.run_limits') }}</legend>
              <label class="flex items-center gap-2 text-xs chrome-text-muted">
                <input v-model="draft.recursiveChecksum" type="checkbox" /> {{ t('compare.limit_recursive_checksum') }}
              </label>
              <label class="flex items-center justify-between text-xs">
                <span class="chrome-text-muted">{{ t('compare.limit_bisection_factor') }}</span>
                <input v-model.number="draft.bisectionFactor" type="number" min="8" max="32" class="chrome-input w-20 text-xs" />
              </label>
              <label class="flex items-center justify-between text-xs">
                <span class="chrome-text-muted">{{ t('compare.limit_bisection_threshold') }}</span>
                <input v-model.number="draft.bisectionThreshold" type="number" class="chrome-input w-24 text-xs" />
              </label>
              <label class="flex items-center justify-between text-xs">
                <span class="chrome-text-muted">{{ t('compare.limit_max_bisection_depth') }}</span>
                <input v-model.number="draft.maxBisectionDepth" type="number" min="0" class="chrome-input w-20 text-xs" />
              </label>
              <label class="flex items-center gap-2 text-xs chrome-text-muted">
                <input v-model="draft.sampleQuickCheck" type="checkbox" /> {{ t('compare.limit_sample_quick_check') }}
              </label>
              <div v-if="draft.sampleQuickCheck" class="pl-5 space-y-2">
                <label class="flex items-center justify-between text-xs">
                  <span class="chrome-text-muted">{{ t('compare.limit_sample_size') }}</span>
                  <input v-model.number="draft.sampleSize" type="number" min="1" class="chrome-input w-20 text-xs" />
                </label>
                <label class="flex items-center justify-between text-xs">
                  <span class="chrome-text-muted">{{ t('compare.limit_sample_confidence') }}</span>
                  <input v-model.number="draft.sampleConfidence" type="number" step="0.01" min="0.01" max="0.99" class="chrome-input w-20 text-xs" />
                </label>
              </div>
              <label class="flex items-center gap-2 text-xs chrome-text-muted">
                <input v-model="draft.streamCompare" type="checkbox" /> {{ t('compare.limit_stream_compare') }}
              </label>
              <label class="flex items-center gap-2 text-xs chrome-text-muted">
                <input v-model="draft.persistSame" type="checkbox" /> {{ t('compare.limit_persist_same') }}
              </label>
            </fieldset>
          </div>

          <div v-if="editorError" class="text-xs text-red-600 dark:text-red-400">{{ editorError }}</div>
          <div class="flex items-center gap-2">
            <button type="button" class="chrome-btn-primary text-sm" :disabled="savingTask" @click="onSaveTask">
              <Save class="w-4 h-4" /> {{ savingTask ? t('compare.saving') : t('compare.save_task') }}
            </button>
            <button
              v-if="activeTask"
              type="button"
              class="chrome-btn-secondary text-sm"
              @click="onDeleteTask"
            >
              <Trash2 class="w-4 h-4" /> {{ t('compare.delete_task') }}
            </button>
          </div>
        </div>

        <!-- ============ 结果 tab ============ -->
        <div v-show="rightTab === 'results'" class="flex flex-col min-h-0 h-full">
          <!-- 进度摘要 -->
          <div class="px-4 py-2 border-b chrome-border-subtle flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] chrome-text-muted">
            <span>{{ t('compare.progress') }}:</span>
            <span>{{ t('compare.progress_scanned') }} {{ progress.scanned_segments ?? 0 }}</span>
            <span>{{ t('compare.progress_skipped') }} {{ progress.skipped_segments ?? 0 }}</span>
            <span>{{ t('compare.progress_skipped_rows') }} {{ progress.skipped_rows ?? 0 }}</span>
            <span>{{ t('compare.progress_row_mode') }} {{ progress.row_mode_segments ?? 0 }}</span>
            <span v-if="progress.max_depth != null">{{ t('compare.progress_max_depth') }} {{ progress.max_depth }}</span>
            <span v-if="sampleResult?.enabled" class="inline-flex items-center gap-1 text-amber-700 dark:text-amber-300">
              <AlertTriangle class="w-3 h-3" /> {{ t('compare.sample_flag') }}
            </span>
          </div>

          <!-- 采样快检卡 -->
          <div
            v-if="sampleResult?.enabled"
            class="m-4 rounded-card border border-amber-300 dark:border-amber-500/40 bg-amber-50 dark:bg-amber-500/10 p-3 text-xs space-y-1"
          >
            <div class="font-medium text-amber-800 dark:text-amber-200 flex items-center gap-1">
              <AlertTriangle class="w-3.5 h-3.5" /> {{ t('compare.sample_card') }}
            </div>
            <div class="text-amber-800 dark:text-amber-200">
              {{ t('compare.sample_confidence_bound', { confidence: sampleResult.confidence ?? '-', bound: sampleResult.difference_rate_upper_bound ?? '-' }) }}
            </div>
            <div class="text-amber-700 dark:text-amber-300">
              {{ t('compare.sample_n', { n: sampleResult.sampled_rows ?? 0 }) }} ·
              {{ t('compare.sample_diffs', { count: sampleResult.observed_differences ?? 0 }) }}
            </div>
            <div class="text-[11px] italic text-amber-700 dark:text-amber-300">{{ t('compare.sample_not_full') }}</div>
          </div>

          <!-- 4 桶 tab(徽章计数) -->
          <div class="px-4 pt-3 flex flex-wrap items-center gap-2">
            <button
              v-for="bucket in COMPARE_BUCKETS"
              :key="bucket"
              type="button"
              class="inline-flex items-center gap-2 rounded-card border px-3 py-1.5 text-xs font-medium transition-colors"
              :class="
                resultBucket === bucket
                  ? BUCKET_STYLE[bucket].active
                  : 'chrome-border chrome-text-muted hover:chrome-bg-elevated'
              "
              @click="onSelectBucket(bucket)"
            >
              <span class="w-2 h-2 rounded-full" :class="BUCKET_STYLE[bucket].dot" />
              {{ t(`compare.bucket_${bucket}`) }}
              <span class="tabular-nums">{{ bucketCounts[bucket] ?? 0 }}</span>
            </button>
          </div>

          <div class="flex-1 min-h-0 overflow-auto p-4">
            <div v-if="resultError" class="text-xs text-red-600 dark:text-red-400">{{ resultError }}</div>
            <div v-else-if="resultLoading" class="chrome-text-muted text-sm"><LoadingDots /></div>
            <div v-else-if="!resultData || resultData.rows.length === 0" class="chrome-text-muted text-sm">
              {{ t('compare.bucket_empty') }}
            </div>

            <!-- diff tab:单元格分裂(主键列固定左侧) -->
            <table v-else class="text-xs border chrome-border rounded-card overflow-hidden">
              <thead class="chrome-bg-elevated">
                <tr class="text-left chrome-text-muted">
                  <th
                    v-for="kc in keyColumns"
                    :key="`k-${kc}`"
                    class="px-3 py-2 font-medium sticky left-0 chrome-bg-elevated border-r chrome-border"
                  >
                    <span class="inline-flex items-center gap-1">
                      <span class="text-[10px] uppercase chrome-accent">{{ t('compare.pk_column') }}</span> {{ kc }}
                    </span>
                  </th>
                  <th
                    v-for="col in compareColumns"
                    :key="`c-${col}`"
                    class="px-3 py-2 font-medium"
                  >
                    {{ col }}
                  </th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(row, ri) in resultData.rows" :key="ri" class="border-t chrome-border-subtle align-top">
                  <td
                    v-for="kc in keyColumns"
                    :key="`k-${ri}-${kc}`"
                    class="px-3 py-2 font-mono chrome-text-heading sticky left-0 chrome-bg-panel border-r chrome-border"
                  >
                    {{ fmtCell(row.pk[kc]) }}
                  </td>
                  <td
                    v-for="col in compareColumns"
                    :key="`c-${ri}-${col}`"
                    class="px-3 py-2 font-mono"
                  >
                    <!-- diff 桶:有 cell 差异 → 上下分裂(红删 / 绿增);否则单行 -->
                    <template v-if="resultBucket === 'diff' && cellDiffFor(row, col)">
                      <div class="text-red-600 dark:text-red-400 line-through">
                        {{ fmtCell(cellDiffFor(row, col)?.source) }}
                      </div>
                      <div class="text-emerald-600 dark:text-emerald-400">
                        {{ fmtCell(cellDiffFor(row, col)?.target) }}
                      </div>
                    </template>
                    <template v-else-if="resultBucket === 'only_source'">
                      <span class="chrome-text-heading">{{ fmtCell(row.source?.[col]) }}</span>
                    </template>
                    <template v-else-if="resultBucket === 'only_target'">
                      <span class="chrome-text-heading">{{ fmtCell(row.target?.[col]) }}</span>
                    </template>
                    <template v-else>
                      <span class="chrome-text-muted">{{ fmtCell(row.source?.[col] ?? row.target?.[col]) }}</span>
                    </template>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <!-- ============ 画像 tab ============ -->
        <div v-show="rightTab === 'profile'" class="p-4 space-y-5 max-w-3xl">
          <div v-if="profileError" class="text-xs text-red-600 dark:text-red-400">{{ profileError }}</div>
          <div v-else-if="profileLoading" class="chrome-text-muted text-sm"><LoadingDots /></div>
          <div v-else-if="!diffProfile.generated && profileColumns.length === 0" class="chrome-text-muted text-sm">
            {{ t('compare.profile_empty') }}
          </div>
          <template v-else>
            <!-- 每列差异率条形图 + 系统性偏差 chip -->
            <div>
              <div class="text-xs font-medium chrome-text-heading mb-2">{{ t('compare.profile_column_rates') }}</div>
              <div v-for="col in profileColumns" :key="col.name" class="mb-3">
                <div class="flex items-center justify-between text-xs mb-1">
                  <span class="font-mono chrome-text-heading">{{ col.name }}</span>
                  <span class="chrome-text-muted">
                    {{ t('compare.profile_changed_of', { changed: col.info.changed_rows ?? 0, observed: col.info.observed_rows ?? 0 }) }}
                    · {{ diffRatePct(col.info.diff_rate) }}%
                  </span>
                </div>
                <div class="h-2 rounded-full chrome-bg-elevated overflow-hidden">
                  <div class="h-full rounded-full bg-amber-500" :style="{ width: diffRatePct(col.info.diff_rate) + '%' }" />
                </div>
                <div v-if="col.info.numeric_delta?.systematic_offset" class="mt-1">
                  <span class="inline-flex items-center gap-1 rounded-input px-1.5 py-0.5 text-[10px] font-medium bg-sky-50 text-sky-700 dark:bg-sky-500/15 dark:text-sky-300">
                    {{ t('compare.profile_systematic') }}:
                    {{ t('compare.profile_constant_offset', { value: col.info.numeric_delta?.constant_offset ?? '-' }) }}
                  </span>
                </div>
              </div>
            </div>

            <!-- 缺失行主键区间聚类 -->
            <div>
              <div class="text-xs font-medium chrome-text-heading mb-2">{{ t('compare.profile_missing_ranges') }}</div>
              <div v-if="missingSource.length === 0 && missingTarget.length === 0" class="text-xs chrome-text-muted">
                {{ t('compare.profile_no_missing') }}
              </div>
              <div v-else class="grid grid-cols-2 gap-3">
                <div>
                  <div class="text-[11px] text-red-700 dark:text-red-300 mb-1">{{ t('compare.profile_missing_source') }}</div>
                  <div v-for="(rng, i) in missingSource" :key="`ms-${i}`" class="text-[11px] font-mono chrome-text-muted">
                    {{ fmtCell(rng) }}
                  </div>
                </div>
                <div>
                  <div class="text-[11px] text-emerald-700 dark:text-emerald-300 mb-1">{{ t('compare.profile_missing_target') }}</div>
                  <div v-for="(rng, i) in missingTarget" :key="`mt-${i}`" class="text-[11px] font-mono chrome-text-muted">
                    {{ fmtCell(rng) }}
                  </div>
                </div>
              </div>
            </div>

            <!-- AI 归因(叠加层,非前置依赖) -->
            <div class="border-t chrome-border-subtle pt-4">
              <button type="button" class="chrome-btn-secondary text-sm" :disabled="aiBusy" @click="onAiExplain">
                <Bot class="w-4 h-4" :class="aiBusy && 'animate-pulse'" />
                {{ aiBusy ? t('compare.ai_explaining') : t('compare.ai_explain') }}
              </button>
              <div v-if="aiResult" class="mt-3">
                <div v-if="aiResult.ok" class="rounded-card border chrome-border p-3 text-sm chrome-text-normal whitespace-pre-wrap">
                  <div class="text-xs chrome-text-muted mb-1">
                    {{ t('compare.ai_result') }}
                    <span v-if="aiResult.provider"> · {{ aiResult.provider }}/{{ aiResult.model }}</span>
                    · {{ t('compare.ai_egress', { level: aiResult.egress_level }) }}
                  </div>
                  {{ aiResult.attribution }}
                </div>
                <div v-else class="flex items-center gap-2 text-xs chrome-text-muted">
                  <X class="w-4 h-4" /> {{ t('compare.ai_disabled') }}
                  <span v-if="aiResult.error" class="font-mono">({{ aiResult.error }})</span>
                </div>
              </div>
            </div>
          </template>
        </div>
      </div>
    </section>
  </div>
</template>
