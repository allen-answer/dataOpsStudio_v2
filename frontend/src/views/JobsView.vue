<script setup lang="ts">
/**
 * JobsView —— /projects/:id/jobs
 *
 * 列出当前 user 在当前 project 下的 jobs(后端 owner_user_id filter)。
 * 顶栏:状态 chip 过滤(全部 / pending / running / success / failed / cancelled / timeout)。
 * 行点击:抽屉显示 job 详情;success 时拉前 100 行结果。
 *
 * 1.5s 轮询列表 —— 当有 pending/running job 时持续轮,全部终态则停。
 */
import { computed, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import { ListChecks, X, AlertTriangle } from 'lucide-vue-next'
import { listJobs, getJobResult, cancelJob, type JobResultResponse } from '../api/jobs'
import { ApiError, type JobListItem, type JobStatus } from '../api/types'
import JobStatusBadge from '../components/JobStatusBadge.vue'
import ResultTable from '../components/ResultTable.vue'
import EmptyState from '../components/EmptyState.vue'
import LoadingDots from '../components/LoadingDots.vue'

const { t } = useI18n()
const route = useRoute()
const qc = useQueryClient()

const projectId = computed(() =>
  typeof route.params.id === 'string' ? route.params.id : '',
)

const filterStatus = ref<JobStatus | 'all'>('all')

const queryKey = computed(() => ['jobs', projectId.value, filterStatus.value])
const query = useQuery({
  queryKey,
  queryFn: () =>
    listJobs({
      project_id: projectId.value,
      status: filterStatus.value === 'all' ? undefined : filterStatus.value,
      limit: 100,
    }),
  enabled: computed(() => Boolean(projectId.value)),
})

const jobs = computed<JobListItem[]>(() => query.data.value ?? [])

// ─── 轮询(只要有 pending / running 就 1.5s 拉一次)──────────
const hasActive = computed(() =>
  jobs.value.some((j) => j.status === 'pending' || j.status === 'running'),
)
let pollTimer: ReturnType<typeof setInterval> | null = null
watch(
  hasActive,
  (active) => {
    if (active && pollTimer === null) {
      pollTimer = setInterval(() => {
        void qc.invalidateQueries({ queryKey: queryKey.value })
      }, 1500)
    }
    if (!active && pollTimer !== null) {
      clearInterval(pollTimer)
      pollTimer = null
    }
  },
  { immediate: true },
)
onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
})

// ─── 抽屉(行 drill-down)──────────────────────────────
const drawerJob = ref<JobListItem | null>(null)
const drawerResult = ref<JobResultResponse | null>(null)
const drawerError = ref<string | null>(null)
const drawerLoading = ref(false)
const PAGE_SIZE = 100

async function openDrawer(j: JobListItem): Promise<void> {
  drawerJob.value = j
  drawerResult.value = null
  drawerError.value = null
  if (j.status === 'success') {
    await fetchDrawerPage(0)
  }
}
function closeDrawer(): void {
  drawerJob.value = null
  drawerResult.value = null
  drawerError.value = null
}
async function fetchDrawerPage(offset: number): Promise<void> {
  if (!drawerJob.value) return
  drawerLoading.value = true
  try {
    drawerResult.value = await getJobResult(drawerJob.value.id, offset, PAGE_SIZE)
  } catch (e) {
    drawerError.value = e instanceof ApiError ? e.message : t('common.error_unknown')
  } finally {
    drawerLoading.value = false
  }
}

async function onCancel(j: JobListItem): Promise<void> {
  try {
    await cancelJob(j.id)
    await qc.invalidateQueries({ queryKey: queryKey.value })
  } catch (e) {
    drawerError.value = e instanceof ApiError ? e.message : t('common.error_unknown')
  }
}

// ─── 展示工具 ───────────────────────────────────────────
const STATUSES: Array<JobStatus | 'all'> = [
  'all',
  'pending',
  'running',
  'success',
  'failed',
  'cancelled',
  'timeout',
]

function formatDate(iso: string | null): string {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleString(undefined, {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    })
  } catch {
    return iso
  }
}

function duration(j: JobListItem): string {
  if (!j.started_at) return '—'
  const start = new Date(j.started_at).getTime()
  const end = j.finished_at ? new Date(j.finished_at).getTime() : Number.NaN
  if (Number.isNaN(end)) return '—'
  const ms = end - start
  if (ms < 1000) return `${ms} ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`
  return `${Math.floor(ms / 60_000)} min`
}

function errorMessage(): string {
  const e = query.error.value
  if (e instanceof ApiError) return e.message || t('common.error_unknown')
  return t('common.error_unknown')
}
</script>

<template>
  <div class="max-w-7xl mx-auto px-6 lg:px-10 py-8 w-full">
    <!-- header -->
    <div class="mb-6">
      <div class="text-xs uppercase tracking-wider chrome-text-muted font-medium">
        {{ t('nav.projects') }} / {{ projectId.slice(0, 8) }}
      </div>
      <h1 class="text-h2 font-semibold tracking-tight chrome-text-heading mt-1">
        {{ t('jobs.title') }}
      </h1>
    </div>

    <!-- 状态 chip filter -->
    <div class="flex flex-wrap gap-2 mb-4">
      <button
        v-for="s in STATUSES"
        :key="s"
        type="button"
        @click="filterStatus = s"
        class="px-3 py-1 rounded-input text-xs font-medium transition-colors"
        :class="
          filterStatus === s
            ? 'chrome-accent-bg text-white'
            : 'chrome-bg-panel chrome-border border chrome-text-muted hover:chrome-bg-elevated'
        "
      >
        {{ s === 'all' ? t('jobs.all') : t(`jobs.status_${s}`) }}
      </button>
    </div>

    <!-- loading -->
    <div
      v-if="query.isLoading.value"
      class="flex items-center justify-center gap-2 py-12 text-sm chrome-text-muted"
    >
      <LoadingDots />
      <span>{{ t('common.loading') }}</span>
    </div>

    <!-- error -->
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

    <!-- empty -->
    <div
      v-else-if="jobs.length === 0"
      class="chrome-bg-panel border chrome-border rounded-card"
    >
      <EmptyState
        :icon="ListChecks"
        :title="t('jobs.empty_title')"
        :hint="t('jobs.empty_hint')"
      />
    </div>

    <!-- table -->
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
            <th class="font-medium py-2 px-3">ID</th>
            <th class="font-medium py-2 px-3">{{ t('jobs.col_kind') }}</th>
            <th class="font-medium py-2 px-3">{{ t('jobs.col_status') }}</th>
            <th class="font-medium py-2 px-3">{{ t('jobs.col_created_at') }}</th>
            <th class="font-medium py-2 px-3">{{ t('jobs.col_duration') }}</th>
            <th class="font-medium py-2 px-3 text-right">{{ t('jobs.col_actions') }}</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="j in jobs"
            :key="j.id"
            class="border-b chrome-border-subtle last:border-b-0 cursor-pointer hover:chrome-bg-elevated transition-colors"
            @click="openDrawer(j)"
          >
            <td class="py-2 px-3 font-mono text-xs chrome-text-normal">
              {{ j.id.slice(0, 8) }}
            </td>
            <td class="py-2 px-3 chrome-text-normal">{{ j.kind }}</td>
            <td class="py-2 px-3">
              <JobStatusBadge :status="j.status" />
            </td>
            <td class="py-2 px-3 chrome-text-muted tabular-nums">
              {{ formatDate(j.created_at) }}
            </td>
            <td class="py-2 px-3 chrome-text-muted tabular-nums">
              {{ duration(j) }}
            </td>
            <td class="py-2 px-3 text-right">
              <button
                v-if="j.status === 'pending' || j.status === 'running'"
                type="button"
                class="chrome-btn-secondary"
                @click.stop="onCancel(j)"
              >
                {{ t('common.cancel') }}
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 抽屉 -->
    <Transition
      enter-active-class="transition duration-150 ease-out"
      enter-from-class="opacity-0"
      enter-to-class="opacity-100"
      leave-active-class="transition duration-100 ease-in"
      leave-from-class="opacity-100"
      leave-to-class="opacity-0"
    >
      <div
        v-if="drawerJob"
        class="fixed inset-0 z-40 flex justify-end"
        style="background-color: rgb(0 0 0 / 0.45);"
        @click.self="closeDrawer"
      >
        <div
          class="w-full max-w-3xl h-full chrome-bg-panel border-l chrome-border flex flex-col"
        >
          <!-- 抽屉 head -->
          <div
            class="flex items-start justify-between px-6 pt-5 pb-4 border-b chrome-border-subtle"
          >
            <div>
              <div class="text-xs chrome-text-muted font-mono">
                job {{ drawerJob.id }}
              </div>
              <div class="flex items-center gap-2 mt-1">
                <h2 class="text-section font-semibold chrome-text-heading">
                  {{ drawerJob.kind }}
                </h2>
                <JobStatusBadge :status="drawerJob.status" size="md" />
              </div>
              <div class="text-xs chrome-text-muted mt-1">
                {{ formatDate(drawerJob.created_at) }} · {{ duration(drawerJob) }}
              </div>
            </div>
            <button
              type="button"
              @click="closeDrawer"
              class="chrome-btn-ghost"
              aria-label="close"
            >
              <X class="w-4 h-4" />
            </button>
          </div>

          <!-- error 行 -->
          <div
            v-if="drawerJob.error"
            class="px-6 py-3 border-b chrome-border-subtle text-sm font-mono whitespace-pre-wrap"
            style="background-color: rgb(239 68 68 / 0.08); color: rgb(185 28 28);"
          >
            {{ drawerJob.error }}
          </div>
          <div
            v-if="drawerError"
            class="px-6 py-3 border-b chrome-border-subtle text-sm"
            style="background-color: rgb(239 68 68 / 0.08); color: rgb(185 28 28);"
          >
            <AlertTriangle class="w-4 h-4 inline mr-1" />
            {{ drawerError }}
          </div>

          <!-- 结果 -->
          <div class="flex-1 min-h-0">
            <div
              v-if="drawerJob.status !== 'success'"
              class="h-full grid place-items-center chrome-text-muted text-sm"
            >
              {{ t('jobs.no_result_hint') }}
            </div>
            <div
              v-else-if="drawerLoading && !drawerResult"
              class="h-full grid place-items-center chrome-text-muted text-sm"
            >
              <LoadingDots />
            </div>
            <ResultTable
              v-else-if="drawerResult"
              :columns="drawerResult.columns"
              :rows="drawerResult.rows"
              :offset="drawerResult.offset"
              :limit="drawerResult.limit"
              :loaded-rows="drawerResult.loaded_rows"
              :truncated="drawerResult.truncated"
              @change-page="fetchDrawerPage"
            />
          </div>
        </div>
      </div>
    </Transition>
  </div>
</template>
