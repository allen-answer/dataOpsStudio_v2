<script setup lang="ts">
/**
 * SqlWorkspaceView —— /projects/:id/sql
 *
 * 上半:datasource 选 + Monaco SQL editor + Execute / Cancel
 * 下半:状态条 + ResultTable(成功后)
 *
 * Monaco 在本 view 首次挂载时 lazy 加载;/login /projects 不付包体代价。
 *
 * 后端约束:只接 readonly SELECT/WITH;非 SELECT 返 400 invalid_sql,
 * 表单里红色提示。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useQuery } from '@tanstack/vue-query'
import { storeToRefs } from 'pinia'
import { Play, Square, AlertTriangle } from 'lucide-vue-next'
// 只引 editor 核心 + SQL 语言贡献(避免拉全部 ~80 个语言进包)
import * as monaco from 'monaco-editor/esm/vs/editor/editor.api'
import 'monaco-editor/esm/vs/basic-languages/sql/sql.contribution'
import editorWorker from 'monaco-editor/esm/vs/editor/editor.worker?worker'
import { VueMonacoEditor, loader } from '@guolao/vue-monaco-editor'
import { listDatasources } from '../api/datasources'
import { executeSql } from '../api/sql'
import { getJobResult, type JobResultResponse } from '../api/jobs'
import { ApiError, type DatasourceListItem } from '../api/types'
import { useThemeStore } from '../stores/theme'
import { useJobPoll } from '../composables/useJobPoll'
import JobStatusBadge from '../components/JobStatusBadge.vue'
import ResultTable from '../components/ResultTable.vue'
import LoadingDots from '../components/LoadingDots.vue'

// ─── Monaco 初始化(只在 SqlWorkspaceView 首次 mount 时执行)───
//
// vite 把 editorWorker 作为 ?worker 入口编译,monaco 主包 lazy split 出来。
// loader.config 必须在第一次渲染 <VueMonacoEditor> 前调用。
//
// MonacoEnvironment 用 generic editor worker 兜底 —— 不开启语言专属 worker
// (我们只用 SQL,内置 tokenizer 够用,无需 typescript / json / css worker)。
const g = self as unknown as { MonacoEnvironment?: { getWorker: () => Worker } }
if (!g.MonacoEnvironment) {
  g.MonacoEnvironment = { getWorker: () => new editorWorker() }
}
loader.config({ monaco })

const { t } = useI18n()
const route = useRoute()
const themeStore = useThemeStore()
const { variant } = storeToRefs(themeStore)

const projectId = computed(() =>
  typeof route.params.id === 'string' ? route.params.id : '',
)

// ─── datasource picker ──────────────────────────────────
const dsQuery = useQuery({
  queryKey: computed(() => ['datasources', projectId.value]),
  queryFn: () => listDatasources(projectId.value),
  enabled: computed(() => Boolean(projectId.value)),
})
const datasources = computed<DatasourceListItem[]>(() => dsQuery.data.value ?? [])
const selectedDsId = ref<string>('')

watch(datasources, (list) => {
  if (!selectedDsId.value && list.length > 0) selectedDsId.value = list[0].id
})

// ─── 非 MySQL 兜底 ──────────────────────────────────────
// 2.0.0 worker 仅实现 MySQL adapter(app/worker.py:402),非 MySQL 数据源执行必 failed。
// 选到非 MySQL 时:执行按钮禁用 + 顶部提示,避免用户提交后才看到 failed。
// ★ 后端补多方言 adapter 后移除此兜底(见 backlog「T7 §4 SQL 跑非 MySQL」)。
const selectedDs = computed<DatasourceListItem | undefined>(() =>
  datasources.value.find((d) => d.id === selectedDsId.value),
)
const unsupportedDb = computed<string | null>(() => {
  const ds = selectedDs.value
  return ds && ds.db_type !== 'mysql' ? ds.db_type : null
})

// ─── editor state ───────────────────────────────────────
const sql = ref<string>('SELECT 1 AS hello;')
const editorTheme = computed(() => {
  const v = variant.value
  return v === 'spotify-dark' || v === 'figma-dark' ? 'vs-dark' : 'vs'
})

// ─── execute / poll / result ────────────────────────────
const { state: pollState, polling, start: startPoll, cancel: cancelPoll, reset: resetPoll } =
  useJobPoll()

const execError = ref<string | null>(null)
const result = ref<JobResultResponse | null>(null)
const resultLoading = ref(false)
const PAGE_SIZE = 100

// 切换数据源 = 新上下文,清掉上一条执行级错误(含非 MySQL 拦截 / SQL guard / 网络)。
watch(selectedDsId, () => {
  execError.value = null
})

async function onExecute(): Promise<void> {
  if (!selectedDsId.value || !sql.value.trim()) {
    execError.value = t('sql.error_pick_ds_or_sql')
    return
  }
  // 非 MySQL 兜底:Cmd+Enter 直接调本函数,绕过按钮 disabled,这里再拦一次。
  if (unsupportedDb.value) {
    execError.value = t('sql.unsupported_db_error', { db: unsupportedDb.value })
    return
  }
  execError.value = null
  result.value = null
  resetPoll()
  try {
    const res = await executeSql({
      datasource_id: selectedDsId.value,
      sql: sql.value,
    })
    startPoll(res.job_id)
  } catch (e) {
    if (e instanceof ApiError) {
      execError.value = e.message || t('common.error_unknown')
    } else {
      execError.value = t('common.error_unknown')
    }
  }
}

watch(
  () => pollState.status,
  async (s) => {
    if (s === 'success' && pollState.jobId) {
      await fetchPage(0)
    }
  },
)

async function fetchPage(offset: number): Promise<void> {
  if (!pollState.jobId) return
  resultLoading.value = true
  try {
    result.value = await getJobResult(pollState.jobId, offset, PAGE_SIZE)
  } catch (e) {
    if (e instanceof ApiError) execError.value = e.message
    else execError.value = t('common.error_unknown')
  } finally {
    resultLoading.value = false
  }
}

function onChangePage(offset: number): void {
  void fetchPage(offset)
}

async function onCancel(): Promise<void> {
  try {
    await cancelPoll()
  } catch {
    /* error already in poll state */
  }
}

onMounted(() => {
  if (datasources.value.length > 0) selectedDsId.value = datasources.value[0].id
})

// keyboard:Cmd/Ctrl + Enter 执行
function onEditorMount(editor: monaco.editor.IStandaloneCodeEditor): void {
  editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.Enter, () => {
    void onExecute()
  })
}

const isTerminal = computed(
  () =>
    pollState.status === 'success' ||
    pollState.status === 'failed' ||
    pollState.status === 'cancelled' ||
    pollState.status === 'timeout',
)
</script>

<template>
  <div class="flex flex-col h-full chrome-bg-main">
    <!-- 顶栏 -->
    <div class="flex items-center gap-3 px-6 py-3 border-b chrome-border chrome-bg-panel">
      <div class="text-xs uppercase tracking-wider chrome-text-muted font-medium">
        {{ t('nav.projects') }} / {{ projectId.slice(0, 8) }} / {{ t('nav.sql') }}
      </div>
      <div class="flex-1" />
      <!-- datasource picker -->
      <select
        v-model="selectedDsId"
        class="chrome-input"
        :disabled="dsQuery.isLoading.value || datasources.length === 0"
      >
        <option v-if="datasources.length === 0" disabled value="">
          {{ t('sql.no_datasource') }}
        </option>
        <option v-for="ds in datasources" :key="ds.id" :value="ds.id">
          {{ ds.name }} ({{ ds.db_type }})
        </option>
      </select>
      <!-- execute / cancel -->
      <button
        v-if="!polling"
        type="button"
        class="chrome-btn-primary"
        @click="onExecute"
        :disabled="!selectedDsId || !sql.trim() || !!unsupportedDb"
        :title="unsupportedDb ? t('sql.unsupported_db_error', { db: unsupportedDb }) : ''"
      >
        <Play class="w-3.5 h-3.5" />
        {{ t('sql.execute') }}
      </button>
      <button
        v-else
        type="button"
        class="chrome-btn-secondary"
        @click="onCancel"
        :disabled="pollState.cancelling"
      >
        <Square class="w-3.5 h-3.5" />
        {{ pollState.cancelling ? t('sql.cancelling') : t('sql.cancel') }}
      </button>
    </div>

    <!-- 非 MySQL 兜底提示(2.0 worker 仅支持 MySQL)-->
    <div
      v-if="unsupportedDb"
      class="flex items-center gap-2 px-6 py-2 text-xs border-b chrome-border-subtle"
      style="background-color: rgb(180 83 9 / 0.10); color: rgb(180 83 9);"
    >
      <AlertTriangle class="w-3.5 h-3.5 shrink-0" />
      <span>{{ t('sql.unsupported_db_warn', { db: unsupportedDb }) }}</span>
    </div>

    <!-- editor 上半 -->
    <div class="flex-1 min-h-[35vh] border-b chrome-border relative">
      <VueMonacoEditor
        v-model:value="sql"
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
        }"
        @mount="onEditorMount"
      />
    </div>

    <!-- 结果下半 -->
    <div class="flex-1 min-h-[35vh] flex flex-col">
      <!-- 状态条 -->
      <div
        class="flex items-center justify-between px-6 py-2 border-b chrome-border-subtle text-xs"
      >
        <div class="flex items-center gap-3">
          <span class="chrome-text-muted uppercase tracking-wider font-medium">
            {{ t('sql.result') }}
          </span>
          <JobStatusBadge v-if="pollState.status" :status="pollState.status" />
          <span v-if="pollState.message" class="chrome-text-muted">
            {{ pollState.message }}
          </span>
        </div>
        <span v-if="pollState.jobId" class="chrome-text-muted font-mono text-[10px]">
          job: {{ pollState.jobId.slice(0, 8) }}
        </span>
      </div>

      <!-- 执行级错误(SQL guard 400 / network 等) -->
      <div
        v-if="execError"
        class="flex items-start gap-2 px-6 py-3 border-b chrome-border-subtle text-sm"
        style="background-color: rgb(239 68 68 / 0.08); color: rgb(185 28 28);"
      >
        <AlertTriangle class="w-4 h-4 shrink-0 mt-0.5" />
        <span>{{ execError }}</span>
      </div>

      <!-- 任务级错误 -->
      <div
        v-else-if="pollState.status === 'failed' && pollState.error"
        class="flex items-start gap-2 px-6 py-3 border-b chrome-border-subtle text-sm"
        style="background-color: rgb(239 68 68 / 0.08); color: rgb(185 28 28);"
      >
        <AlertTriangle class="w-4 h-4 shrink-0 mt-0.5" />
        <span class="font-mono whitespace-pre-wrap">{{ pollState.error }}</span>
      </div>

      <!-- 占位 / 进行中 / 终态 -->
      <div class="flex-1 flex flex-col min-h-0">
        <div
          v-if="!pollState.jobId"
          class="flex-1 grid place-items-center chrome-text-muted text-sm"
        >
          {{ t('sql.run_to_see_result') }}
        </div>
        <div
          v-else-if="!isTerminal"
          class="flex-1 grid place-items-center chrome-text-muted text-sm"
        >
          <div class="flex items-center gap-2">
            <LoadingDots />
            <span>{{ t('sql.running_hint') }}</span>
          </div>
        </div>
        <div
          v-else-if="pollState.status === 'cancelled'"
          class="flex-1 grid place-items-center chrome-text-muted text-sm"
        >
          {{ t('sql.cancelled_hint') }}
        </div>
        <div
          v-else-if="pollState.status === 'timeout'"
          class="flex-1 grid place-items-center text-sm"
          style="color: rgb(180 83 9);"
        >
          {{ t('sql.timeout_hint') }}
        </div>
        <div
          v-else-if="pollState.status === 'success' && resultLoading && !result"
          class="flex-1 grid place-items-center chrome-text-muted text-sm"
        >
          <LoadingDots />
        </div>
        <ResultTable
          v-else-if="pollState.status === 'success' && result"
          :columns="result.columns"
          :rows="result.rows"
          :offset="result.offset"
          :limit="result.limit"
          :loaded-rows="result.loaded_rows"
          :truncated="result.truncated"
          @change-page="onChangePage"
        />
      </div>
    </div>
  </div>
</template>
