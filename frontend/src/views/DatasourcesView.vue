<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useQuery, useMutation, useQueryClient } from '@tanstack/vue-query'
import {
  Database,
  Plus,
  AlertTriangle,
  CheckCircle2,
  XCircle,
  Zap,
  Lock,
  Pencil,
  Trash2,
  ChevronDown,
  ShieldCheck,
} from 'lucide-vue-next'
import {
  listDatasources,
  getDatasource,
  createDatasource,
  updateDatasource,
  deleteDatasource,
  testDatasource,
} from '../api/datasources'
import {
  ApiError,
  DEFAULT_OPERATION_POLICY,
  type DatasourceDeleteBlocked,
  type DatasourceListItem,
  type DatasourceTestErrorCode,
  type DbType,
  type OperationPolicy,
} from '../api/types'
import JobStatusBadge from '../components/JobStatusBadge.vue'
import EmptyState from '../components/EmptyState.vue'
import LoadingDots from '../components/LoadingDots.vue'
import Modal from '../components/Modal.vue'

const { t } = useI18n()
const route = useRoute()
const qc = useQueryClient()

const projectId = computed(() =>
  typeof route.params.id === 'string' ? route.params.id : '',
)

const queryKey = computed(() => ['datasources', projectId.value])

const query = useQuery({
  queryKey,
  queryFn: () => listDatasources(projectId.value),
  enabled: computed(() => Boolean(projectId.value)),
})

const datasources = computed<DatasourceListItem[]>(() => query.data.value ?? [])

// ─── 测连接(单条 inline)──────────────────────────────────
type TestState = 'idle' | 'pending' | 'ok' | 'failed'
const testStates = reactive<Record<string, TestState>>({})
const testErrors = reactive<Record<string, string>>({}) // 已分类的 hover 文案
const testOk = reactive<Record<string, string>>({}) // 成功摘要:✓ MySQL 8.0 · 235 ms

// DatasourceTestErrorCode → i18n key(细化文案,不暴露 driver raw error,见 PRD §3 失败表)
const TEST_ERROR_I18N: Record<DatasourceTestErrorCode, string> = {
  auth_failed: 'datasources.test_err_auth_failed',
  host_unreachable: 'datasources.test_err_host_unreachable',
  timeout: 'datasources.test_err_timeout',
  permission_denied: 'datasources.test_err_permission_denied',
  unknown: 'datasources.test_err_unknown',
}

function testFailedText(code: DatasourceTestErrorCode | null): string {
  return t(code ? TEST_ERROR_I18N[code] : 'datasources.test_err_unknown')
}

async function onTest(ds: DatasourceListItem): Promise<void> {
  testStates[ds.id] = 'pending'
  delete testErrors[ds.id]
  delete testOk[ds.id]
  try {
    const res = await testDatasource(ds.id)
    if (res.ok) {
      testStates[ds.id] = 'ok'
      const parts = [res.server_version, res.latency_ms != null ? `${res.latency_ms} ms` : null]
        .filter(Boolean)
        .join(' · ')
      testOk[ds.id] = parts
    } else {
      testStates[ds.id] = 'failed'
      testErrors[ds.id] = testFailedText(res.error_code)
    }
  } catch (e) {
    testStates[ds.id] = 'failed'
    testErrors[ds.id] = e instanceof ApiError ? e.message : t('common.error_unknown')
  }
}

// ─── 表单(新建 / 编辑共用)─────────────────────────────────
const modalOpen = ref(false)
// null = 新建;否则 = 正在编辑的 datasource(只读元信息,字段值在 form 里)
const editingId = ref<string | null>(null)
const isEditing = computed(() => editingId.value !== null)
const editLoading = ref(false) // 编辑时拉 GET /datasources/{id} 详情的 loading

interface FormState {
  name: string
  db_type: DbType
  host: string
  port: number
  username: string
  database: string
  password: string
  environment: string
  operation_policy: OperationPolicy
}

const initialForm: FormState = {
  name: '',
  db_type: 'mysql',
  host: '127.0.0.1',
  port: 3306,
  username: '',
  database: '',
  password: '',
  environment: 'sandbox',
  operation_policy: { ...DEFAULT_OPERATION_POLICY },
}

const form = reactive<FormState>({
  ...initialForm,
  operation_policy: { ...DEFAULT_OPERATION_POLICY },
})
const formError = ref<string | null>(null)
const policyOpen = ref(false) // 权限折叠面板默认收起

// ─── 8 个 allow_* 开关(顺序 + 文案 + 生效标 按 PRD §3 权限面板)──
// 2.0.0 仅 SELECT / EXPLAIN 真生效;其余开关旁标 "2.1+"。
interface PolicyToggle {
  key: keyof OperationPolicy
  effective: boolean // true = 2.0.0 已生效;false = 标 2.1+
}
const POLICY_TOGGLES: PolicyToggle[] = [
  { key: 'allow_select', effective: true },
  { key: 'allow_explain', effective: true },
  { key: 'allow_oracle_plan_table', effective: false },
  { key: 'allow_dm_explain', effective: false },
  { key: 'allow_schema_import', effective: false },
  { key: 'allow_schema_save', effective: false },
  { key: 'allow_scenario_write', effective: false },
  { key: 'allow_record_task', effective: false },
]

// ─── environment 三档(+ unknown)──────────────────────────
// 后端 environment 是自由字符串、不做枚举校验,枚举锁定在前端:
// 与 PRD §3 + figma showcase EnvBadge 对齐(unknown / sandbox / staging / prod)。
const ENVIRONMENTS = ['unknown', 'sandbox', 'staging', 'prod'] as const

// 选 prod 需二次确认:第一次点「创建」只 arm,第二次才真提交(见 onSubmit)。
const prodArmed = ref(false)
watch(
  () => form.environment,
  (env) => {
    if (env !== 'prod') prodArmed.value = false
  },
)

// ─── 端口随 db_type 自动默认 ───────────────────────────────
// 仅当用户没手动改过端口(当前值仍是某个已知默认)时跟随切换,
// 避免覆盖用户手填的自定义端口。
const DB_PORT_DEFAULTS: Record<DbType, number> = {
  mysql: 3306,
  postgresql: 5432,
  oracle: 1521,
  dm: 5236,
  db2: 50000,
}
const KNOWN_DEFAULT_PORTS = new Set(Object.values(DB_PORT_DEFAULTS))
watch(
  () => form.db_type,
  (next) => {
    if (KNOWN_DEFAULT_PORTS.has(Number(form.port))) {
      form.port = DB_PORT_DEFAULTS[next]
    }
  },
)

function resetForm(): void {
  Object.assign(form, initialForm)
  form.operation_policy = { ...DEFAULT_OPERATION_POLICY }
  formError.value = null
  prodArmed.value = false
  policyOpen.value = false
}

function openModal(): void {
  editingId.value = null
  resetForm()
  modalOpen.value = true
}

// 编辑:开 modal → 拉详情 → 回填(密码字段始终留空 = 不改)。
async function openEditModal(ds: DatasourceListItem): Promise<void> {
  editingId.value = ds.id
  resetForm()
  modalOpen.value = true
  editLoading.value = true
  formError.value = null
  try {
    const detail = await getDatasource(ds.id)
    form.name = detail.name
    form.db_type = detail.db_type
    form.host = detail.host
    form.port = detail.port
    form.username = detail.username
    form.database = detail.database ?? ''
    form.environment = detail.environment || 'unknown'
    form.password = '' // 留空 = 不改密码
    form.operation_policy = { ...DEFAULT_OPERATION_POLICY, ...detail.operation_policy }
  } catch (e) {
    formError.value =
      e instanceof ApiError
        ? e.status === 0
          ? t('common.error_network')
          : e.message
        : t('common.error_unknown')
  } finally {
    editLoading.value = false
  }
}

function closeModal(): void {
  modalOpen.value = false
  editingId.value = null
}

const createMutation = useMutation({
  mutationFn: () =>
    createDatasource({
      project_id: projectId.value,
      name: form.name.trim(),
      db_type: form.db_type,
      host: form.host.trim(),
      port: Number(form.port),
      username: form.username.trim(),
      database: form.database.trim(),
      password: form.password,
      environment: form.environment || 'sandbox',
      extra: {},
      operation_policy: { ...form.operation_policy },
    }),
  onSuccess: async () => {
    await qc.invalidateQueries({ queryKey: queryKey.value })
    closeModal()
  },
})

const updateMutation = useMutation({
  mutationFn: () => {
    const id = editingId.value
    if (id === null) throw new Error('no datasource being edited')
    return updateDatasource(id, {
      name: form.name.trim(),
      db_type: form.db_type,
      host: form.host.trim(),
      port: Number(form.port),
      username: form.username.trim(),
      database: form.database.trim(),
      // 密码留空 = 不改(后端 `if body.password:`);只在用户填了才下发。
      ...(form.password ? { password: form.password } : {}),
      environment: form.environment || 'unknown',
      operation_policy: { ...form.operation_policy },
    })
  },
  onSuccess: async () => {
    await qc.invalidateQueries({ queryKey: queryKey.value })
    closeModal()
  },
})

const submitting = computed(
  () => createMutation.isPending.value || updateMutation.isPending.value,
)

async function onSubmit(): Promise<void> {
  formError.value = null
  if (editLoading.value) return
  // 编辑时密码可留空(= 不改);新建时密码必填。
  const passwordRequired = !isEditing.value
  if (
    !form.name.trim() ||
    !form.host.trim() ||
    !form.username.trim() ||
    !form.database.trim() ||
    (passwordRequired && !form.password)
  ) {
    formError.value = t('datasources.error_missing_field')
    return
  }
  // prod 二次确认:第一次点提交只 arm,不提交。
  if (form.environment === 'prod' && !prodArmed.value) {
    prodArmed.value = true
    return
  }
  try {
    if (isEditing.value) await updateMutation.mutateAsync()
    else await createMutation.mutateAsync()
  } catch (e) {
    if (e instanceof ApiError) {
      formError.value = e.status === 0 ? t('common.error_network') : e.message
    } else {
      formError.value = t('common.error_unknown')
    }
  }
}

// ─── 删除(确认弹窗 → DELETE;409 列引用清单)──────────────
const deleteTarget = ref<DatasourceListItem | null>(null)
const deleteBlocked = ref<DatasourceDeleteBlocked | null>(null)
const deleteError = ref<string | null>(null)

function openDeleteModal(ds: DatasourceListItem): void {
  deleteTarget.value = ds
  deleteBlocked.value = null
  deleteError.value = null
}
function closeDeleteModal(): void {
  deleteTarget.value = null
  deleteBlocked.value = null
  deleteError.value = null
}

const deleteMutation = useMutation({
  mutationFn: (id: string) => deleteDatasource(id),
  onSuccess: async () => {
    await qc.invalidateQueries({ queryKey: queryKey.value })
    closeDeleteModal()
  },
})

async function onConfirmDelete(): Promise<void> {
  const ds = deleteTarget.value
  if (!ds) return
  deleteError.value = null
  deleteBlocked.value = null
  try {
    await deleteMutation.mutateAsync(ds.id)
  } catch (e) {
    if (e instanceof ApiError && e.status === 409) {
      // 被既有 job 引用:后端返 DatasourceDeleteBlocked 体(error/message/references)
      deleteBlocked.value = (e.body as DatasourceDeleteBlocked | undefined) ?? null
      if (!deleteBlocked.value) deleteError.value = e.message
    } else if (e instanceof ApiError) {
      deleteError.value = e.status === 0 ? t('common.error_network') : e.message
    } else {
      deleteError.value = t('common.error_unknown')
    }
  }
}

// ─── 展示工具 ──────────────────────────────────────────────
function countMessage(n: number): string {
  if (n === 0) return t('datasources.count_zero')
  if (n === 1) return t('datasources.count_one')
  return t('datasources.count_other', { count: n })
}

function formatDate(iso: string): string {
  try {
    const d = new Date(iso)
    return d.toLocaleString(undefined, {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    })
  } catch {
    return iso
  }
}

function errorMessage(): string {
  const e = query.error.value
  if (e instanceof ApiError) {
    if (e.status === 0) return t('common.error_network')
    return e.message || t('common.error_unknown')
  }
  return t('common.error_unknown')
}

// ─── DbTypeBadge:每个 db_type 配一个色系(Tailwind class)─────
// 注:不依赖 chrome token,因为 db_type 是数据语义、不是 chrome 语义。
const DB_TYPE_STYLE: Record<DbType, string> = {
  mysql:
    'bg-sky-50 text-sky-700 dark:bg-sky-500/15 dark:text-sky-300 border border-sky-200/60 dark:border-sky-500/30',
  postgresql:
    'bg-indigo-50 text-indigo-700 dark:bg-indigo-500/15 dark:text-indigo-300 border border-indigo-200/60 dark:border-indigo-500/30',
  oracle:
    'bg-rose-50 text-rose-700 dark:bg-rose-500/15 dark:text-rose-300 border border-rose-200/60 dark:border-rose-500/30',
  dm:
    'bg-amber-50 text-amber-700 dark:bg-amber-500/15 dark:text-amber-300 border border-amber-200/60 dark:border-amber-500/30',
  db2:
    'bg-teal-50 text-teal-700 dark:bg-teal-500/15 dark:text-teal-300 border border-teal-200/60 dark:border-teal-500/30',
}
const DB_TYPE_LABEL: Record<DbType, string> = {
  mysql: 'MySQL',
  postgresql: 'PostgreSQL',
  oracle: 'Oracle',
  dm: 'DM',
  db2: 'DB2',
}

// ─── EnvBadge ──────────────────────────────────────────────
// list 端点(GET /datasources)现已返回 environment + environment_verified。
// 视觉规则见 PRD §3 / D.7:prod 红 + 🔒;environment_verified=true 额外加 ✓(admin 显式 verified)。
function envOf(ds: DatasourceListItem): string | undefined {
  return ds.environment || undefined
}
function isProd(env: string | undefined): boolean {
  return env?.toLowerCase() === 'prod' || env?.toLowerCase() === 'production'
}
function envBadgeClass(env: string | undefined): string {
  if (isProd(env)) {
    return 'bg-red-50 text-red-700 dark:bg-red-500/15 dark:text-red-400 border border-red-200/60 dark:border-red-500/40'
  }
  if (env?.toLowerCase() === 'uat' || env?.toLowerCase() === 'staging') {
    return 'bg-amber-50 text-amber-700 dark:bg-amber-500/15 dark:text-amber-300 border border-amber-200/60 dark:border-amber-500/30'
  }
  return 'bg-slate-50 text-slate-600 dark:bg-slate-500/15 dark:text-slate-300 border border-slate-200/60 dark:border-slate-500/30'
}

const DB_TYPES: DbType[] = ['mysql', 'postgresql', 'oracle', 'dm', 'db2']
</script>

<template>
  <div class="px-6 lg:px-10 py-8 w-full">
    <!-- Header -->
    <div class="flex items-end justify-between mb-6">
      <div>
        <div class="text-xs uppercase tracking-wider chrome-text-muted font-medium">
          {{ t('nav.projects') }} / {{ projectId.slice(0, 8) }}
        </div>
        <h1 class="text-h2 font-semibold tracking-tight chrome-text-heading mt-1">
          {{ t('datasources.title') }}
        </h1>
        <div
          v-if="!query.isLoading.value && !query.isError.value"
          class="text-sm chrome-text-muted mt-1"
        >
          {{ countMessage(datasources.length) }}
        </div>
      </div>
      <button type="button" @click="openModal" class="chrome-btn-primary">
        <Plus class="w-4 h-4" />
        {{ t('datasources.new') }}
      </button>
    </div>

    <!-- Loading -->
    <div
      v-if="query.isLoading.value"
      class="flex items-center justify-center gap-2 py-12 text-sm chrome-text-muted"
    >
      <LoadingDots />
      <span>{{ t('common.loading') }}</span>
    </div>

    <!-- Error -->
    <div
      v-else-if="query.isError.value"
      class="border border-red-200 dark:border-red-500/30 bg-red-50 dark:bg-red-500/10 rounded-card p-5 flex items-start gap-3"
    >
      <AlertTriangle class="w-5 h-5 text-red-500 dark:text-red-400 shrink-0 mt-0.5" />
      <div>
        <div class="text-sm font-medium text-red-700 dark:text-red-400">{{ t('common.error') }}</div>
        <div class="text-sm text-red-600 dark:text-red-300 mt-0.5">{{ errorMessage() }}</div>
        <button
          @click="query.refetch()"
          type="button"
          class="text-xs text-red-700 dark:text-red-400 underline mt-2"
        >
          {{ t('common.retry') }}
        </button>
      </div>
    </div>

    <!-- Empty -->
    <div
      v-else-if="datasources.length === 0"
      class="chrome-bg-panel border chrome-border rounded-card"
      style="box-shadow: var(--shadow-card);"
    >
      <EmptyState
        :icon="Database"
        :title="t('datasources.empty_title')"
        :hint="t('datasources.empty_hint')"
      />
    </div>

    <!-- Table -->
    <div
      v-else
      class="chrome-bg-panel border chrome-border rounded-card overflow-hidden"
      style="box-shadow: var(--shadow-card);"
    >
      <table class="w-full text-data">
        <thead>
          <tr
            class="text-left text-xs chrome-text-muted border-b chrome-border-subtle"
            style="background-color: rgb(var(--bg-panel-elevated) / 0.4);"
          >
            <th class="font-medium py-2 px-3">{{ t('datasources.col_name') }}</th>
            <th class="font-medium py-2 px-3">{{ t('datasources.col_db_type') }}</th>
            <th class="font-medium py-2 px-3">{{ t('datasources.col_environment') }}</th>
            <th class="font-medium py-2 px-3">{{ t('datasources.col_host') }}</th>
            <th class="font-medium py-2 px-3">{{ t('datasources.col_database') }}</th>
            <th class="font-medium py-2 px-3">{{ t('datasources.col_created_at') }}</th>
            <th class="font-medium py-2 px-3 text-right">{{ t('datasources.col_actions') }}</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="ds in datasources"
            :key="ds.id"
            class="border-b chrome-border-subtle last:border-b-0 group transition-colors"
          >
            <!-- 名称 + id -->
            <td class="py-2 px-3">
              <div class="font-medium chrome-text-heading">{{ ds.name }}</div>
              <div class="text-xs font-mono chrome-text-muted mt-0.5">
                {{ ds.id.slice(0, 8) }}
              </div>
            </td>
            <!-- 数据库类型(色块 badge) -->
            <td class="py-2 px-3">
              <span
                class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-input text-xs font-medium"
                :class="DB_TYPE_STYLE[ds.db_type]"
              >
                {{ DB_TYPE_LABEL[ds.db_type] }}
              </span>
            </td>
            <!-- env badge(prod 必带 🔒;verified 加 ✓)-->
            <td class="py-2 px-3">
              <span
                v-if="envOf(ds)"
                class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-input text-xs font-medium uppercase tracking-wider"
                :class="envBadgeClass(envOf(ds))"
              >
                <Lock v-if="isProd(envOf(ds))" class="w-3 h-3" />
                {{ envOf(ds) }}
                <CheckCircle2
                  v-if="ds.environment_verified"
                  class="w-3 h-3"
                  :title="t('datasources.env_verified_title')"
                />
              </span>
              <span v-else class="chrome-text-muted text-xs">—</span>
            </td>
            <!-- host:port -->
            <td class="py-2 px-3 font-mono chrome-text-normal">
              {{ ds.host }}:{{ ds.port }}
            </td>
            <!-- database -->
            <td class="py-2 px-3 font-mono chrome-text-normal">
              {{ ds.database || '—' }}
            </td>
            <!-- created_at -->
            <td class="py-2 px-3 chrome-text-muted tabular-nums">
              {{ formatDate(ds.created_at) }}
            </td>
            <!-- 操作(hover 才出现 + 测连接结果常驻)-->
            <td class="py-2 px-3">
              <div class="flex items-center justify-end gap-2 min-h-[1.75rem]">
                <!-- 测连接结果(常驻)-->
                <span
                  v-if="testStates[ds.id] === 'ok'"
                  class="text-xs text-emerald-600 dark:text-emerald-400 inline-flex items-center gap-1"
                  :title="testOk[ds.id]"
                >
                  <CheckCircle2 class="w-3.5 h-3.5" />
                  {{ t('datasources.test_ok')
                  }}<template v-if="testOk[ds.id]"> · {{ testOk[ds.id] }}</template>
                </span>
                <span
                  v-else-if="testStates[ds.id] === 'failed'"
                  class="text-xs text-red-600 dark:text-red-400 inline-flex items-center gap-1"
                  :title="testErrors[ds.id]"
                >
                  <XCircle class="w-3.5 h-3.5" />
                  {{ testErrors[ds.id] || t('datasources.test_failed') }}
                </span>
                <span
                  v-else-if="testStates[ds.id] === 'pending'"
                  class="text-xs chrome-text-muted inline-flex items-center gap-1"
                >
                  <LoadingDots />
                  {{ t('datasources.testing') }}
                </span>

                <!-- hover 才出现的图标操作行 -->
                <div
                  class="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity"
                >
                  <button
                    type="button"
                    @click="onTest(ds)"
                    :disabled="testStates[ds.id] === 'pending'"
                    class="chrome-btn-ghost"
                    :title="t('datasources.test_connection')"
                  >
                    <Zap class="w-3.5 h-3.5" />
                  </button>
                  <button
                    type="button"
                    @click="openEditModal(ds)"
                    class="chrome-btn-ghost"
                    :title="t('common.edit')"
                  >
                    <Pencil class="w-3.5 h-3.5" />
                  </button>
                  <button
                    type="button"
                    @click="openDeleteModal(ds)"
                    class="chrome-btn-ghost hover:!text-red-600 dark:hover:!text-red-400"
                    :title="t('common.delete')"
                  >
                    <Trash2 class="w-3.5 h-3.5" />
                  </button>
                </div>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- Create / Edit modal -->
    <Modal
      :open="modalOpen"
      :title="isEditing ? t('datasources.edit_title') : t('datasources.create_title')"
      :subtitle="isEditing ? t('datasources.edit_subtitle') : t('datasources.create_subtitle')"
      @close="closeModal"
    >
      <div
        v-if="editLoading"
        class="flex items-center justify-center gap-2 py-10 text-sm chrome-text-muted"
      >
        <LoadingDots />
        <span>{{ t('common.loading') }}</span>
      </div>
      <form v-else @submit.prevent="onSubmit" class="space-y-4">
        <!-- 名称 -->
        <div class="space-y-1.5">
          <label class="form-label">{{ t('datasources.field_name') }}</label>
          <input
            v-model="form.name"
            type="text"
            class="chrome-input w-full"
            :placeholder="t('datasources.field_name_placeholder')"
            :disabled="submitting"
            autocomplete="off"
          />
        </div>

        <!-- DB type + 环境 -->
        <div class="grid grid-cols-2 gap-3">
          <div class="space-y-1.5">
            <label class="form-label">{{ t('datasources.field_db_type') }}</label>
            <select
              v-model="form.db_type"
              class="chrome-input w-full"
              :disabled="submitting"
            >
              <option v-for="t in DB_TYPES" :key="t" :value="t">{{ DB_TYPE_LABEL[t] }}</option>
            </select>
          </div>
          <div class="space-y-1.5">
            <label class="form-label">{{ t('datasources.field_environment') }}</label>
            <select
              v-model="form.environment"
              class="chrome-input w-full"
              :disabled="submitting"
            >
              <option v-for="env in ENVIRONMENTS" :key="env" :value="env">
                {{ t(`datasources.env_${env}`) }}
              </option>
            </select>
          </div>
        </div>

        <!-- Host + Port -->
        <div class="grid grid-cols-[1fr_100px] gap-3">
          <div class="space-y-1.5">
            <label class="form-label">{{ t('datasources.field_host') }}</label>
            <input
              v-model="form.host"
              type="text"
              class="chrome-input w-full"
              :placeholder="t('datasources.field_host_placeholder')"
              :disabled="submitting"
              autocomplete="off"
            />
          </div>
          <div class="space-y-1.5">
            <label class="form-label">{{ t('datasources.field_port') }}</label>
            <input
              v-model.number="form.port"
              type="number"
              class="chrome-input w-full tabular-nums"
              min="1"
              max="65535"
              :disabled="submitting"
            />
          </div>
        </div>

        <!-- 用户名 -->
        <div class="space-y-1.5">
          <label class="form-label">{{ t('datasources.field_username') }}</label>
          <input
            v-model="form.username"
            type="text"
            class="chrome-input w-full"
            :disabled="submitting"
            autocomplete="off"
          />
        </div>

        <!-- 数据库名 -->
        <div class="space-y-1.5">
          <label class="form-label">{{ t('datasources.field_database') }}</label>
          <input
            v-model="form.database"
            type="text"
            class="chrome-input w-full"
            :placeholder="t('datasources.field_database_placeholder')"
            :disabled="submitting"
            autocomplete="off"
          />
        </div>

        <!-- 密码(编辑时留空 = 不改)-->
        <div class="space-y-1.5">
          <label class="form-label">{{ t('datasources.field_password') }}</label>
          <input
            v-model="form.password"
            type="password"
            class="chrome-input w-full"
            :placeholder="
              isEditing
                ? t('datasources.field_password_edit_placeholder')
                : t('datasources.field_password_placeholder')
            "
            :disabled="submitting"
            autocomplete="new-password"
          />
        </div>

        <!-- prod 二次确认警示(选 prod 时常驻;arm 后文案升级)-->
        <div
          v-if="form.environment === 'prod'"
          class="flex items-start gap-2 rounded-input px-3 py-2 text-xs"
          style="background-color: rgb(239 68 68 / 0.08); color: rgb(185 28 28);"
        >
          <Lock class="w-4 h-4 shrink-0 mt-0.5" />
          <span>
            {{ prodArmed ? t('datasources.prod_confirm_hint_armed') : t('datasources.prod_confirm_hint') }}
          </span>
        </div>

        <!-- 编辑改名 / 改类型 → 旧连接信息失效,会以新信息重连 -->
        <div
          v-if="isEditing"
          class="flex items-start gap-2 rounded-input px-3 py-2 text-xs chrome-text-muted"
          style="background-color: rgb(var(--bg-panel-elevated) / 0.5);"
        >
          <AlertTriangle class="w-3.5 h-3.5 shrink-0 mt-0.5 text-amber-500" />
          <span>{{ t('datasources.edit_reconnect_hint') }}</span>
        </div>

        <!-- 权限折叠面板(8 个 allow_*;默认收起,默认值与后端一致:仅 SELECT 开)-->
        <div class="rounded-input border chrome-border-subtle overflow-hidden">
          <button
            type="button"
            class="w-full flex items-center justify-between px-3 py-2 text-xs font-medium chrome-text-normal hover:chrome-bg-elevated transition-colors"
            @click="policyOpen = !policyOpen"
            :aria-expanded="policyOpen"
          >
            <span class="inline-flex items-center gap-1.5">
              <ShieldCheck class="w-3.5 h-3.5 text-sky-500" />
              {{ t('datasources.policy_title') }}
            </span>
            <ChevronDown
              class="w-4 h-4 transition-transform"
              :class="{ 'rotate-180': policyOpen }"
            />
          </button>
          <div v-show="policyOpen" class="px-3 pb-3 pt-1 space-y-2 border-t chrome-border-subtle">
            <p class="text-xs chrome-text-muted pt-1">
              {{ t('datasources.policy_hint') }}
            </p>
            <label
              v-for="tg in POLICY_TOGGLES"
              :key="tg.key"
              class="flex items-center gap-2.5 py-1 cursor-pointer select-none"
            >
              <input
                type="checkbox"
                v-model="form.operation_policy[tg.key]"
                :disabled="submitting"
                class="shrink-0 accent-sky-500 w-3.5 h-3.5"
              />
              <span class="text-xs chrome-text-normal flex items-center gap-1.5 flex-wrap">
                {{ t(`datasources.policy_${tg.key}`) }}
                <span
                  v-if="!tg.effective"
                  class="px-1 py-px rounded text-[10px] font-medium bg-slate-100 text-slate-500 dark:bg-slate-500/20 dark:text-slate-400"
                >
                  2.1+
                </span>
              </span>
            </label>
          </div>
        </div>

        <!-- 错误 -->
        <div v-if="formError" class="text-xs text-red-500 dark:text-red-400">
          {{ formError }}
        </div>

        <!-- 操作 -->
        <div class="flex justify-end gap-2 pt-2">
          <button
            type="button"
            @click="closeModal"
            class="chrome-btn-secondary"
            :disabled="submitting"
          >
            {{ t('common.cancel') }}
          </button>
          <button
            type="submit"
            class="chrome-btn-primary"
            :class="{ 'chrome-btn-danger': form.environment === 'prod' && prodArmed }"
            :disabled="submitting"
          >
            <template v-if="submitting">
              <LoadingDots />
              <span>{{ t('common.submitting') }}</span>
            </template>
            <span v-else>
              {{
                form.environment === 'prod' && prodArmed
                  ? t('datasources.prod_confirm_button')
                  : isEditing
                    ? t('common.save')
                    : t('datasources.create_submit')
              }}
            </span>
          </button>
        </div>
      </form>
    </Modal>

    <!-- Delete confirm modal -->
    <Modal
      :open="deleteTarget !== null"
      :title="t('datasources.delete_title')"
      @close="closeDeleteModal"
    >
      <div v-if="deleteTarget" class="space-y-4">
        <!-- 409:被任务引用,不能删除(列引用清单)-->
        <template v-if="deleteBlocked">
          <div
            class="flex items-start gap-2 rounded-input px-3 py-2.5 text-sm"
            style="background-color: rgb(239 68 68 / 0.08); color: rgb(185 28 28);"
          >
            <AlertTriangle class="w-4 h-4 shrink-0 mt-0.5" />
            <span>{{ t('datasources.delete_blocked') }}</span>
          </div>
          <div class="rounded-input border chrome-border-subtle overflow-hidden">
            <div
              class="px-3 py-1.5 text-xs chrome-text-muted border-b chrome-border-subtle"
              style="background-color: rgb(var(--bg-panel-elevated) / 0.4);"
            >
              {{ t('datasources.delete_blocked_count', { count: deleteBlocked.references.length }) }}
            </div>
            <ul class="max-h-52 overflow-y-auto divide-y chrome-border-subtle">
              <li
                v-for="ref in deleteBlocked.references"
                :key="ref.job_id"
                class="flex items-center gap-2 px-3 py-1.5 text-xs"
              >
                <span class="font-mono chrome-text-normal">{{ ref.job_id.slice(0, 8) }}</span>
                <span class="chrome-text-muted">{{ ref.kind }}</span>
                <span class="ml-auto"><JobStatusBadge :status="ref.status" /></span>
              </li>
            </ul>
          </div>
        </template>

        <!-- 确认提示(尚未拒绝)-->
        <p v-else class="text-sm chrome-text-normal">
          {{ t('datasources.delete_confirm', { name: deleteTarget.name }) }}
        </p>

        <div v-if="deleteError" class="text-xs text-red-500 dark:text-red-400">
          {{ deleteError }}
        </div>

        <div class="flex justify-end gap-2 pt-1">
          <button
            type="button"
            @click="closeDeleteModal"
            class="chrome-btn-secondary"
            :disabled="deleteMutation.isPending.value"
          >
            {{ deleteBlocked ? t('common.close') : t('common.cancel') }}
          </button>
          <button
            v-if="!deleteBlocked"
            type="button"
            @click="onConfirmDelete"
            class="chrome-btn-primary chrome-btn-danger"
            :disabled="deleteMutation.isPending.value"
          >
            <template v-if="deleteMutation.isPending.value">
              <LoadingDots />
              <span>{{ t('common.submitting') }}</span>
            </template>
            <span v-else>{{ t('datasources.delete_submit') }}</span>
          </button>
        </div>
      </div>
    </Modal>
  </div>
</template>

<style scoped>
.form-label {
  @apply block text-xs uppercase tracking-wider font-medium;
  color: rgb(var(--text-muted));
}
</style>
