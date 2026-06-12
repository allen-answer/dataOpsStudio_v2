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
  BookOpen,
  Database,
  FileText,
  History,
  Pencil,
  Pin,
  PinOff,
  Play,
  Plus,
  Save,
  Search,
  Square,
  Trash2,
  X,
} from 'lucide-vue-next'
import * as monaco from 'monaco-editor/esm/vs/editor/editor.api'
import 'monaco-editor/esm/vs/basic-languages/sql/sql.contribution'
import editorWorker from 'monaco-editor/esm/vs/editor/editor.worker?worker'
import { VueMonacoEditor, loader } from '@guolao/vue-monaco-editor'
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
  type SqlTemplateUpdateRequest,
} from '../api/sql'
import {
  cancelJob,
  getJob,
  getJobResult,
  type JobResponse,
  type JobResultResponse,
} from '../api/jobs'
import { ApiError, type DatasourceListItem, type JobStatus } from '../api/types'
import { useThemeStore } from '../stores/theme'
import { useAuthStore } from '../stores/auth'
import JobStatusBadge from '../components/JobStatusBadge.vue'
import ResultTable from '../components/ResultTable.vue'
import LoadingDots from '../components/LoadingDots.vue'
import Modal from '../components/Modal.vue'

const g = self as unknown as { MonacoEnvironment?: { getWorker: () => Worker } }
if (!g.MonacoEnvironment) {
  g.MonacoEnvironment = { getWorker: () => new editorWorker() }
}
loader.config({ monaco })

const TERMINAL: ReadonlySet<JobStatus> = new Set<JobStatus>([
  'success',
  'failed',
  'cancelled',
  'timeout',
])
const ACTIVE: ReadonlySet<JobStatus> = new Set<JobStatus>(['pending', 'running'])
const PAGE_SIZE = 1000
const POLL_MS = 500
const SAVE_DEBOUNCE_MS = 650

type SidebarTab = 'consoles' | 'history' | 'templates' | 'metadata'
type HistoryRange = 'all' | 'today' | '7d'

interface ConsoleRuntime {
  jobId: string | null
  resultSetId: string | null
  status: JobStatus | null
  error: string | null
  message: string | null
  cancelling: boolean
  result: JobResultResponse | null
  resultLoading: boolean
  pageOffset: number
  startedAt: number | null
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

const { t } = useI18n()
const route = useRoute()
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
const suppressConsoleSave = ref(false)

const runtimes = reactive<Record<string, ConsoleRuntime>>({})
const pollTimers = new Map<string, ReturnType<typeof setTimeout>>()
const saveTimers = new Map<string, ReturnType<typeof setTimeout>>()
const pendingConsolePatches = new Map<string, SqlConsoleUpdateRequest>()
const nowMs = ref(Date.now())
let nowTimer: ReturnType<typeof setInterval> | null = null

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

const activeConsole = computed<SqlConsole | null>(
  () => consoles.value.find((item) => item.id === activeConsoleId.value) ?? null,
)
const activeRuntime = computed<ConsoleRuntime | null>(() =>
  activeConsoleId.value ? runtimeFor(activeConsoleId.value) : null,
)
const selectedDs = computed<DatasourceListItem | undefined>(() =>
  datasources.value.find((d) => d.id === selectedDsId.value),
)
const unsupportedDb = computed<string | null>(() => {
  const ds = selectedDs.value
  return ds && ds.db_type !== 'mysql' ? ds.db_type : null
})
const editorTheme = computed(() => {
  const v = variant.value
  return v === 'spotify-dark' || v === 'figma-dark' ? 'vs-dark' : 'vs'
})
const editorReadOnly = computed(() => {
  const status = activeRuntime.value?.status
  return status ? ACTIVE.has(status) : false
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
      rows,
      seconds: elapsedSeconds(rt),
    })
  }
  if (rt.status === 'cancelled') return t('sql.cancelled_hint')
  if (rt.status === 'timeout') return t('sql.timeout_hint')
  return rt.error || t('jobs.error.sql_failed')
})
const shouldShowResultTable = computed(() => {
  const rt = activeRuntime.value
  if (!rt?.result) return false
  if (rt.status && TERMINAL.has(rt.status)) return true
  return rt.result.rows.length > 0 || rt.result.columns.length > 0
})

watch(datasources, (list) => {
  if (!selectedDsId.value && list.length > 0) selectedDsId.value = list[0].id
})

watch(activeConsole, (consoleRow) => {
  suppressConsoleSave.value = true
  editorSql.value = consoleRow?.sql ?? 'SELECT 1 AS hello;'
  selectedDsId.value = consoleRow?.datasource_id ?? datasources.value[0]?.id ?? ''
  void nextTick(() => {
    suppressConsoleSave.value = false
  })
})

watch(editorSql, (value) => {
  if (suppressConsoleSave.value || !activeConsole.value || editorReadOnly.value) return
  patchLocalConsole(activeConsole.value.id, { sql: value })
  scheduleConsolePatch(activeConsole.value.id, { sql: value })
})

watch(selectedDsId, (value) => {
  execError.value = null
  if (suppressConsoleSave.value || !activeConsole.value || editorReadOnly.value) return
  const datasourceId = value || null
  patchLocalConsole(activeConsole.value.id, { datasource_id: datasourceId })
  scheduleConsolePatch(activeConsole.value.id, { datasource_id: datasourceId })
})

watch(sidebarTab, (tab) => {
  if (tab === 'history') void loadHistory()
  if (tab === 'templates') void loadTemplates()
})
watch([historyDatasourceId, historyRange], () => {
  if (sidebarTab.value === 'history') void loadHistory()
})
watch(templateSearch, () => {
  if (sidebarTab.value === 'templates') void loadTemplates()
})

onMounted(async () => {
  nowTimer = setInterval(() => {
    nowMs.value = Date.now()
  }, 500)
  await loadConsoles()
})

onUnmounted(() => {
  for (const timer of pollTimers.values()) clearTimeout(timer)
  for (const timer of saveTimers.values()) clearTimeout(timer)
  if (nowTimer !== null) clearInterval(nowTimer)
})

function runtimeFor(consoleId: string): ConsoleRuntime {
  if (!runtimes[consoleId]) {
    runtimes[consoleId] = {
      jobId: null,
      resultSetId: null,
      status: null,
      error: null,
      message: null,
      cancelling: false,
      result: null,
      resultLoading: false,
      pageOffset: 0,
      startedAt: null,
    }
  }
  return runtimes[consoleId]
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
  consoleError.value = null
  try {
    await deleteSqlConsole(consoleId)
    consoles.value = consoles.value.filter((item) => item.id !== consoleId)
    stopConsolePoll(consoleId)
    delete runtimes[consoleId]
    if (activeConsoleId.value === consoleId) {
      activeConsoleId.value = sortedConsoles.value[0]?.id ?? null
    }
  } catch (e) {
    consoleError.value = errorMessage(e)
  }
}

async function togglePinned(consoleRow: SqlConsole): Promise<void> {
  await patchConsole(consoleRow.id, { pinned: !consoleRow.pinned })
}

function startRename(consoleRow: SqlConsole): void {
  renameConsoleId.value = consoleRow.id
  renameConsoleDraft.value = consoleRow.name
}

async function submitRename(): Promise<void> {
  if (!renameConsoleId.value || !renameConsoleDraft.value.trim()) return
  await patchConsole(renameConsoleId.value, { name: renameConsoleDraft.value.trim() })
  renameConsoleId.value = null
  renameConsoleDraft.value = ''
}

async function patchConsole(consoleId: string, patch: SqlConsoleUpdateRequest): Promise<void> {
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
  const index = consoles.value.findIndex((item) => item.id === consoleRow.id)
  if (index === -1) consoles.value = [consoleRow, ...consoles.value]
  else consoles.value.splice(index, 1, consoleRow)
}

function nextConsoleName(): string {
  return `query_${consoles.value.length + 1}.sql`
}

async function onExecute(): Promise<void> {
  const consoleRow = activeConsole.value
  if (!consoleRow || !selectedDsId.value || !editorSql.value.trim()) {
    execError.value = t('sql.error_pick_ds_or_sql')
    return
  }
  if (unsupportedDb.value) {
    execError.value = t('sql.unsupported_db_error', { db: unsupportedDb.value })
    return
  }
  execError.value = null
  await flushConsolePatch(consoleRow.id)
  const runtime = runtimeFor(consoleRow.id)
  resetRuntime(runtime)
  runtime.status = 'pending'
  runtime.startedAt = Date.now()
  try {
    const response = await executeSql({
      datasource_id: selectedDsId.value,
      console_id: consoleRow.id,
      sql: editorSql.value,
    })
    runtime.jobId = response.job_id
    runtime.resultSetId = response.result_set_id
    startConsolePoll(consoleRow.id)
    scrollResultIntoView()
  } catch (e) {
    runtime.status = null
    execError.value = errorMessage(e)
    scrollResultIntoView()
  }
}

async function onCancel(): Promise<void> {
  const runtime = activeRuntime.value
  if (!runtime?.jobId || runtime.cancelling) return
  runtime.cancelling = true
  try {
    await cancelJob(runtime.jobId)
  } catch (e) {
    runtime.cancelling = false
    runtime.error = errorMessage(e)
  }
}

function startConsolePoll(consoleId: string): void {
  stopConsolePoll(consoleId)
  void pollConsole(consoleId)
}

function stopConsolePoll(consoleId: string): void {
  const timer = pollTimers.get(consoleId)
  if (timer) clearTimeout(timer)
  pollTimers.delete(consoleId)
}

async function pollConsole(consoleId: string): Promise<void> {
  const runtime = runtimeFor(consoleId)
  if (!runtime.jobId) return
  try {
    const job = await getJob(runtime.jobId)
    applyJob(runtime, job)
    if (job.status === 'pending' || job.status === 'running' || job.status === 'success') {
      await fetchResult(consoleId, runtime.pageOffset, false)
    }
    if (TERMINAL.has(job.status)) {
      stopConsolePoll(consoleId)
      return
    }
  } catch (e) {
    runtime.error = errorMessage(e)
  }
  pollTimers.set(
    consoleId,
    setTimeout(() => {
      void pollConsole(consoleId)
    }, POLL_MS),
  )
}

function applyJob(runtime: ConsoleRuntime, job: JobResponse): void {
  runtime.status = job.status
  runtime.resultSetId = job.result_set_id
  runtime.error = job.error
  runtime.message = job.message
  if (TERMINAL.has(job.status)) runtime.cancelling = false
}

async function fetchResult(
  consoleId: string,
  offset: number,
  showLoading: boolean = true,
): Promise<void> {
  const runtime = runtimeFor(consoleId)
  if (!runtime.jobId) return
  if (showLoading) runtime.resultLoading = true
  try {
    runtime.result = await getJobResult(runtime.jobId, offset, PAGE_SIZE)
    runtime.pageOffset = offset
  } catch (e) {
    if (e instanceof ApiError && e.status === 409 && runtime.status && !TERMINAL.has(runtime.status)) {
      return
    }
    runtime.error = errorMessage(e)
  } finally {
    runtime.resultLoading = false
  }
}

function onChangePage(offset: number): void {
  if (!activeConsoleId.value) return
  void fetchResult(activeConsoleId.value, offset)
}

function resetRuntime(runtime: ConsoleRuntime): void {
  runtime.jobId = null
  runtime.resultSetId = null
  runtime.status = null
  runtime.error = null
  runtime.message = null
  runtime.cancelling = false
  runtime.result = null
  runtime.resultLoading = false
  runtime.pageOffset = 0
  runtime.startedAt = null
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

function scrollResultIntoView(): void {
  void nextTick(() => {
    resultPanel.value?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
  })
}

function onEditorMount(editor: monaco.editor.IStandaloneCodeEditor): void {
  editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.Enter, () => {
    void onExecute()
  })
}

function elapsedSeconds(runtime: ConsoleRuntime): string {
  if (!runtime.startedAt) return '0.0'
  return ((nowMs.value - runtime.startedAt) / 1000).toFixed(1)
}

function formatDate(iso: string | null): string {
  if (!iso) return '-'
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
  <div class="flex h-full min-h-0 chrome-bg-main">
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
          class="chrome-btn-ghost w-full opacity-50"
          disabled
          :title="t('sql.tab_metadata_disabled')"
        >
          <Database class="w-4 h-4" />
        </button>
      </div>

      <div v-if="sidebarTab === 'consoles'" class="flex-1 min-h-0 flex flex-col">
        <div class="flex items-center gap-2 px-3 py-2 border-b chrome-border-subtle">
          <span class="text-xs font-medium uppercase tracking-wider chrome-text-muted flex-1">
            {{ t('sql.tab_consoles') }}
          </span>
          <button type="button" class="chrome-btn-ghost" :title="t('sql.new_console')" @click="createConsole">
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
          <button type="button" class="chrome-btn-primary w-full justify-center" @click="createConsole">
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
                :title="consoleRow.pinned ? t('sql.unpin_console') : t('sql.pin_console')"
                @click.stop="togglePinned(consoleRow)"
              >
                <PinOff v-if="consoleRow.pinned" class="w-3.5 h-3.5" />
                <Pin v-else class="w-3.5 h-3.5" />
              </button>
              <button
                type="button"
                class="chrome-btn-ghost opacity-0 group-hover:opacity-100"
                :title="t('common.edit')"
                @click.stop="startRename(consoleRow)"
              >
                <Pencil class="w-3.5 h-3.5" />
              </button>
              <button
                type="button"
                class="chrome-btn-ghost opacity-0 group-hover:opacity-100"
                :title="t('common.delete')"
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
            :key="item.job_id"
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
            <button v-if="isAdmin" type="button" class="chrome-btn-ghost" :title="t('sql.template_new')" @click="openTemplateCreate">
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
                <button v-if="isAdmin" type="button" class="chrome-btn-ghost" @click.stop="openTemplateEdit(template)">
                  <Pencil class="w-3.5 h-3.5" />
                </button>
                <button v-if="isAdmin" type="button" class="chrome-btn-ghost" @click.stop="removeTemplate(template)">
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
          <button type="button" class="chrome-btn-primary w-full justify-center" @click="applySelectedTemplate">
            <Save class="w-3.5 h-3.5" />
            {{ t('sql.template_insert') }}
          </button>
        </div>
      </div>
    </aside>

    <main class="flex-1 min-w-0 flex flex-col h-full">
      <div class="flex items-center gap-3 px-5 py-3 border-b chrome-border chrome-bg-panel">
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
          v-model="selectedDsId"
          class="chrome-input min-w-[14rem]"
          :disabled="dsQuery.isLoading.value || datasources.length === 0 || editorReadOnly"
        >
          <option v-if="datasources.length === 0" disabled value="">
            {{ t('sql.no_datasource') }}
          </option>
          <option v-for="ds in datasources" :key="ds.id" :value="ds.id">
            {{ ds.name }} ({{ ds.db_type }} · {{ ds.environment }})
          </option>
        </select>
        <button
          v-if="!editorReadOnly"
          type="button"
          class="chrome-btn-primary"
          @click="onExecute"
          :disabled="!activeConsole || !selectedDsId || !editorSql.trim() || !!unsupportedDb"
          :title="unsupportedDb ? t('sql.unsupported_db_error', { db: unsupportedDb }) : ''"
        >
          <Play class="w-3.5 h-3.5" />
          {{ t('sql.execute') }}
        </button>
        <button v-else type="button" class="chrome-btn-secondary" @click="onCancel" :disabled="activeRuntime?.cancelling">
          <Square class="w-3.5 h-3.5" />
          {{ activeRuntime?.cancelling ? t('sql.cancelling') : t('sql.cancel') }}
        </button>
      </div>

      <div
        v-if="unsupportedDb"
        class="flex items-center gap-2 px-5 py-2 text-xs border-b chrome-border-subtle"
        style="background-color: rgb(180 83 9 / 0.10); color: rgb(180 83 9);"
      >
        <AlertTriangle class="w-3.5 h-3.5 shrink-0" />
        <span>{{ t('sql.unsupported_db_warn', { db: unsupportedDb }) }}</span>
      </div>

      <div v-if="!activeConsole" class="flex-1 grid place-items-center">
        <button type="button" class="chrome-btn-primary" @click="createConsole">
          <Plus class="w-4 h-4" />
          {{ t('sql.new_console') }}
        </button>
      </div>
      <template v-else>
        <div class="flex-1 min-h-[35vh] border-b chrome-border relative">
          <VueMonacoEditor
            v-model:value="editorSql"
            language="sql"
            :theme="editorTheme"
            :options="{
              fontSize: 13,
              fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
              minimap: { enabled: false },
              scrollBeyondLastLine: false,
              tabSize: 2,
              wordWrap: 'on',
              renderLineHighlight: 'gutter',
              padding: { top: 12, bottom: 12 },
              readOnly: editorReadOnly,
            }"
            @mount="onEditorMount"
          />
        </div>

        <div ref="resultPanel" class="flex-1 min-h-[35vh] flex flex-col">
          <div class="flex items-center justify-between px-5 py-2 border-b chrome-border-subtle text-xs">
            <div class="flex items-center gap-3 min-w-0">
              <span class="chrome-text-muted uppercase tracking-wider font-medium">
                {{ t('sql.result') }}
              </span>
              <JobStatusBadge v-if="activeRuntime?.status" :status="activeRuntime.status" />
              <span class="chrome-text-muted truncate">{{ statusSummary }}</span>
            </div>
            <span v-if="activeRuntime?.jobId" class="chrome-text-muted font-mono text-[10px]">
              job: {{ activeRuntime.jobId.slice(0, 8) }}
            </span>
          </div>

          <div
            v-if="execError || activeRuntime?.error"
            class="flex items-start gap-2 px-5 py-3 border-b chrome-border-subtle text-sm"
            style="background-color: rgb(239 68 68 / 0.08); color: rgb(185 28 28);"
          >
            <AlertTriangle class="w-4 h-4 shrink-0 mt-0.5" />
            <span class="font-mono whitespace-pre-wrap">{{ execError || activeRuntime?.error }}</span>
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
      </template>
    </main>

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
          <button type="submit" class="chrome-btn-primary" :disabled="templateSaving">
            <Save class="w-3.5 h-3.5" />
            {{ templateSaving ? t('common.submitting') : t('common.save') }}
          </button>
        </div>
      </form>
    </Modal>
  </div>
</template>
