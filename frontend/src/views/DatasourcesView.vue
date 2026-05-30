<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
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
} from 'lucide-vue-next'
import {
  listDatasources,
  createDatasource,
  testDatasource,
} from '../api/datasources'
import { ApiError, type DatasourceListItem, type DbType } from '../api/types'
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
const testErrors = reactive<Record<string, string>>({})

async function onTest(ds: DatasourceListItem): Promise<void> {
  testStates[ds.id] = 'pending'
  delete testErrors[ds.id]
  try {
    const res = await testDatasource(ds.id)
    testStates[ds.id] = res.ok ? 'ok' : 'failed'
    if (!res.ok) testErrors[ds.id] = t('datasources.test_failed')
  } catch (e) {
    testStates[ds.id] = 'failed'
    testErrors[ds.id] = e instanceof ApiError ? e.message : t('common.error_unknown')
  }
}

// ─── 新建表单 ─────────────────────────────────────────────
const modalOpen = ref(false)

interface FormState {
  name: string
  db_type: DbType
  host: string
  port: number
  username: string
  database: string
  password: string
  environment: string
}

const initialForm: FormState = {
  name: '',
  db_type: 'mysql',
  host: '127.0.0.1',
  port: 3306,
  username: '',
  database: '',
  password: '',
  environment: 'dev',
}

const form = reactive<FormState>({ ...initialForm })
const formError = ref<string | null>(null)

function resetForm(): void {
  Object.assign(form, initialForm)
  formError.value = null
}

function openModal(): void {
  resetForm()
  modalOpen.value = true
}

function closeModal(): void {
  modalOpen.value = false
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
      environment: form.environment.trim() || 'dev',
      extra: {},
    }),
  onSuccess: async () => {
    await qc.invalidateQueries({ queryKey: queryKey.value })
    closeModal()
  },
})

async function onSubmit(): Promise<void> {
  formError.value = null
  if (
    !form.name.trim() ||
    !form.host.trim() ||
    !form.username.trim() ||
    !form.database.trim() ||
    !form.password
  ) {
    formError.value = t('datasources.error_missing_field')
    return
  }
  try {
    await createMutation.mutateAsync()
  } catch (e) {
    if (e instanceof ApiError) {
      formError.value = e.status === 0 ? t('common.error_network') : e.message
    } else {
      formError.value = t('common.error_unknown')
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

// ─── EnvBadge:env 从后端 list 暂未返回(★ T7 后补:list response 加 environment)
// 这里用 (ds as any).environment 兜底,后端补字段后直接生效,无需前端二次改。
function envOf(ds: DatasourceListItem): string | undefined {
  return (ds as DatasourceListItem & { environment?: string }).environment
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
  <div class="max-w-6xl mx-auto px-6 lg:px-10 py-8 w-full">
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
            <!-- env badge(prod 必带 🔒)-->
            <td class="py-2 px-3">
              <span
                v-if="envOf(ds)"
                class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-input text-xs font-medium uppercase tracking-wider"
                :class="envBadgeClass(envOf(ds))"
              >
                <Lock v-if="isProd(envOf(ds))" class="w-3 h-3" />
                {{ envOf(ds) }}
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
                >
                  <CheckCircle2 class="w-3.5 h-3.5" />
                  {{ t('datasources.test_ok') }}
                </span>
                <span
                  v-else-if="testStates[ds.id] === 'failed'"
                  class="text-xs text-red-600 dark:text-red-400 inline-flex items-center gap-1"
                  :title="testErrors[ds.id]"
                >
                  <XCircle class="w-3.5 h-3.5" />
                  {{ t('datasources.test_failed') }}
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
                  <!--
                    Edit / Delete 暂不显示:后端无 PUT / DELETE 端点。
                    ★ 后补:T7+1 backend 加 PUT/DELETE /datasources/{id} 后,
                    在此 hover row 内补上 Pencil / Trash2 两个 chrome-btn-ghost。
                  -->
                </div>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- Create modal -->
    <Modal
      :open="modalOpen"
      :title="t('datasources.create_title')"
      :subtitle="t('datasources.create_subtitle')"
      @close="closeModal"
    >
      <form @submit.prevent="onSubmit" class="space-y-4">
        <!-- 名称 -->
        <div class="space-y-1.5">
          <label class="form-label">{{ t('datasources.field_name') }}</label>
          <input
            v-model="form.name"
            type="text"
            class="chrome-input w-full"
            :placeholder="t('datasources.field_name_placeholder')"
            :disabled="createMutation.isPending.value"
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
              :disabled="createMutation.isPending.value"
            >
              <option v-for="t in DB_TYPES" :key="t" :value="t">{{ DB_TYPE_LABEL[t] }}</option>
            </select>
          </div>
          <div class="space-y-1.5">
            <label class="form-label">{{ t('datasources.field_environment') }}</label>
            <input
              v-model="form.environment"
              type="text"
              class="chrome-input w-full"
              :disabled="createMutation.isPending.value"
            />
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
              :disabled="createMutation.isPending.value"
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
              :disabled="createMutation.isPending.value"
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
            :disabled="createMutation.isPending.value"
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
            :disabled="createMutation.isPending.value"
            autocomplete="off"
          />
        </div>

        <!-- 密码 -->
        <div class="space-y-1.5">
          <label class="form-label">{{ t('datasources.field_password') }}</label>
          <input
            v-model="form.password"
            type="password"
            class="chrome-input w-full"
            :placeholder="t('datasources.field_password_placeholder')"
            :disabled="createMutation.isPending.value"
            autocomplete="new-password"
          />
        </div>

        <!--
          ★ 后补:8 个 allow_* 策略折叠面板(allow_select / allow_dml / allow_ddl / allow_truncate /
          allow_grant / allow_drop / allow_create / allow_replace),后端 R8 落地后在此追加 <details> 块。
        -->

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
            :disabled="createMutation.isPending.value"
          >
            {{ t('common.cancel') }}
          </button>
          <button
            type="submit"
            class="chrome-btn-primary"
            :disabled="createMutation.isPending.value"
          >
            <template v-if="createMutation.isPending.value">
              <LoadingDots />
              <span>{{ t('common.submitting') }}</span>
            </template>
            <span v-else>{{ t('datasources.create_submit') }}</span>
          </button>
        </div>
      </form>
    </Modal>
  </div>
</template>

<style scoped>
.form-label {
  @apply block text-xs uppercase tracking-wider font-medium;
  color: rgb(var(--text-muted));
}
</style>
