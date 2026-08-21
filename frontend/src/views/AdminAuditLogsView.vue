<script setup lang="ts">
/**
 * AdminAuditLogsView —— /admin/audit-logs(PRD §10)。
 *
 * 后端:GET /admin/audit-logs?start&end&user_id&action&result&resource_type&limit&offset
 *        → AuditLogItem[](app/api/routes/admin.py)。
 *
 * 过滤器:时间范围 / 用户 / 动作 / 结果 / 对象类型 + 分页(2.0.0 不做 virtual scroll)。
 * 行展开 → JSON detail。
 */
import { computed, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useQuery, keepPreviousData } from '@tanstack/vue-query'
import { ScrollText, AlertTriangle, ChevronDown, ChevronRight, RotateCcw } from 'lucide-vue-next'
import { listAdminAuditLogs } from '../api/admin'
import type { AuditLogItem, AuditLogFilters } from '../api/types'
import EmptyState from '../components/EmptyState.vue'
import LoadingDots from '../components/LoadingDots.vue'
import { createUserErrorMessage } from '../utils/userErrorMessage'

const { t } = useI18n()
const errorMessage = createUserErrorMessage(t)

const PAGE_SIZE = 100

// 过滤器草稿(改后点"应用"才提交,避免每键一查)
const draft = reactive({
  start: '',
  end: '',
  user_id: '',
  action: '',
  result: '',
  resource_type: '',
})
const page = ref(0)

// 实际生效的过滤器(applied)
const applied = reactive<AuditLogFilters>({})

function toIso(local: string): string | undefined {
  if (!local) return undefined
  const d = new Date(local)
  return Number.isNaN(d.getTime()) ? undefined : d.toISOString()
}

function applyFilters(): void {
  page.value = 0
  applied.start = toIso(draft.start)
  applied.end = toIso(draft.end)
  applied.user_id = draft.user_id.trim() || undefined
  applied.action = draft.action.trim() || undefined
  applied.result = draft.result || undefined
  applied.resource_type = draft.resource_type.trim() || undefined
}

function resetFilters(): void {
  draft.start = ''
  draft.end = ''
  draft.user_id = ''
  draft.action = ''
  draft.result = ''
  draft.resource_type = ''
  applyFilters()
}

const queryKey = computed(() => [
  'admin-audit-logs',
  applied.start,
  applied.end,
  applied.user_id,
  applied.action,
  applied.result,
  applied.resource_type,
  page.value,
])

const query = useQuery({
  queryKey,
  queryFn: () => listAdminAuditLogs({ ...applied, limit: PAGE_SIZE, offset: page.value * PAGE_SIZE }),
  placeholderData: keepPreviousData,
})

const logs = computed<AuditLogItem[]>(() => query.data.value ?? [])
const hasNext = computed(() => logs.value.length === PAGE_SIZE)

// 结果枚举(后端 result 自由字符串,常见值锚定为下拉项)
const RESULTS = ['success', 'denied', 'error', 'started'] as const

const expanded = ref<Set<number>>(new Set())
function toggle(id: number): void {
  const next = new Set(expanded.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  expanded.value = next
}

function formatTs(iso: string): string {
  try {
    const d = new Date(iso)
    const base = d.toLocaleString(undefined, {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    })
    return `${base}.${String(d.getMilliseconds()).padStart(3, '0')}`
  } catch {
    return iso
  }
}

const RESULT_STYLE: Record<string, string> = {
  success: 'bg-emerald-500',
  denied: 'bg-amber-500',
  error: 'bg-red-500',
  started: 'bg-slate-400',
}
function resultDot(result: string): string {
  return RESULT_STYLE[result] ?? 'bg-slate-400'
}

const ACTION_STYLE = (action: string): string => {
  if (action.includes('delete')) return 'bg-red-50 text-red-700 dark:bg-red-500/15 dark:text-red-300'
  if (action.includes('create')) return 'bg-emerald-50 text-emerald-700 dark:bg-emerald-500/15 dark:text-emerald-300'
  if (action.includes('update') || action.includes('patch')) return 'bg-amber-50 text-amber-700 dark:bg-amber-500/15 dark:text-amber-300'
  if (action.includes('login') || action.includes('logout')) return 'bg-sky-50 text-sky-700 dark:bg-sky-500/15 dark:text-sky-300'
  return 'bg-slate-50 text-slate-600 dark:bg-slate-500/15 dark:text-slate-300'
}

function objectLabel(log: AuditLogItem): string {
  if (!log.resource_type) return '—'
  const id = log.resource_id ? `:${log.resource_id.slice(0, 8)}` : ''
  return `${log.resource_type}${id}`
}

function copyRequestId(rid: string | null): void {
  if (rid) void navigator.clipboard?.writeText(rid).catch(() => {})
}

</script>

<template>
  <div class="px-6 lg:px-10 py-8 w-full">
    <div class="mb-6">
      <h1 class="text-h2 font-semibold tracking-tight chrome-text-heading">{{ t('admin.audit.title') }}</h1>
      <div class="text-sm chrome-text-muted mt-1">{{ t('admin.audit.subtitle') }}</div>
    </div>

    <!-- 过滤器 -->
    <div class="chrome-bg-panel border chrome-border rounded-card p-4 mb-4">
      <div class="grid grid-cols-2 lg:grid-cols-3 gap-3">
        <div class="space-y-1">
          <label class="filter-label">{{ t('admin.audit.filter_start') }}</label>
          <input v-model="draft.start" type="datetime-local" class="chrome-input w-full" />
        </div>
        <div class="space-y-1">
          <label class="filter-label">{{ t('admin.audit.filter_end') }}</label>
          <input v-model="draft.end" type="datetime-local" class="chrome-input w-full" />
        </div>
        <div class="space-y-1">
          <label class="filter-label">{{ t('admin.audit.filter_user') }}</label>
          <input v-model="draft.user_id" type="text" class="chrome-input w-full font-mono" :placeholder="t('admin.audit.filter_user_ph')" />
        </div>
        <div class="space-y-1">
          <label class="filter-label">{{ t('admin.audit.filter_action') }}</label>
          <input v-model="draft.action" type="text" class="chrome-input w-full" :placeholder="t('admin.audit.filter_action_ph')" />
        </div>
        <div class="space-y-1">
          <label class="filter-label">{{ t('admin.audit.filter_result') }}</label>
          <select v-model="draft.result" class="chrome-input w-full">
            <option value="">{{ t('admin.audit.any') }}</option>
            <option v-for="r in RESULTS" :key="r" :value="r">{{ r }}</option>
          </select>
        </div>
        <div class="space-y-1">
          <label class="filter-label">{{ t('admin.audit.filter_resource_type') }}</label>
          <input v-model="draft.resource_type" type="text" class="chrome-input w-full" :placeholder="t('admin.audit.filter_resource_ph')" />
        </div>
      </div>
      <div class="flex justify-end gap-2 mt-3">
        <button type="button" class="chrome-btn-secondary" @click="resetFilters">
          <RotateCcw class="w-3.5 h-3.5" />{{ t('admin.audit.reset') }}
        </button>
        <button type="button" class="chrome-btn-primary" @click="applyFilters">{{ t('admin.audit.apply') }}</button>
      </div>
    </div>

    <!-- loading -->
    <div v-if="query.isLoading.value" class="flex items-center justify-center gap-2 py-12 text-sm chrome-text-muted">
      <LoadingDots /><span>{{ t('common.loading') }}</span>
    </div>

    <!-- error -->
    <div v-else-if="query.isError.value" class="border border-red-200 dark:border-red-500/30 bg-red-50 dark:bg-red-500/10 rounded-card p-5 flex items-start gap-3">
      <AlertTriangle class="w-5 h-5 text-red-500 shrink-0 mt-0.5" />
      <div>
        <div class="text-sm font-medium text-red-700 dark:text-red-400">{{ t('common.error') }}</div>
        <div class="text-sm text-red-600 dark:text-red-300 mt-0.5">{{ errorMessage(query.error.value) }}</div>
        <button @click="query.refetch()" type="button" class="text-xs text-red-700 dark:text-red-400 underline mt-2">{{ t('common.retry') }}</button>
      </div>
    </div>

    <!-- empty -->
    <div v-else-if="logs.length === 0" class="chrome-bg-panel border chrome-border rounded-card">
      <EmptyState :icon="ScrollText" :title="t('admin.audit.empty_title')" :hint="t('admin.audit.empty_hint')" />
    </div>

    <!-- table -->
    <div v-else class="chrome-bg-panel border chrome-border rounded-card overflow-hidden" style="box-shadow: var(--shadow-card);">
      <table class="w-full text-data">
        <thead>
          <tr class="text-left text-xs chrome-text-muted border-b chrome-border-subtle" style="background-color: rgb(var(--bg-panel-elevated) / 0.4);">
            <th class="font-medium py-2 px-3 w-6"></th>
            <th class="font-medium py-2 px-3">{{ t('admin.audit.col_ts') }}</th>
            <th class="font-medium py-2 px-3">{{ t('admin.audit.col_user') }}</th>
            <th class="font-medium py-2 px-3">{{ t('admin.audit.col_action') }}</th>
            <th class="font-medium py-2 px-3">{{ t('admin.audit.col_object') }}</th>
            <th class="font-medium py-2 px-3">{{ t('admin.audit.col_result') }}</th>
            <th class="font-medium py-2 px-3">{{ t('admin.audit.col_request_id') }}</th>
          </tr>
        </thead>
        <tbody>
          <template v-for="log in logs" :key="log.id">
            <tr
              class="border-b chrome-border-subtle last:border-b-0 cursor-pointer hover:chrome-bg-elevated transition-colors"
              @click="toggle(log.id)"
            >
              <td class="py-2 px-3 chrome-text-muted">
                <component :is="expanded.has(log.id) ? ChevronDown : ChevronRight" class="w-3.5 h-3.5" />
              </td>
              <td class="py-2 px-3 chrome-text-normal tabular-nums font-mono text-xs">{{ formatTs(log.ts) }}</td>
              <td class="py-2 px-3 font-mono text-xs chrome-text-normal">{{ log.user_id ? log.user_id.slice(0, 8) : '—' }}</td>
              <td class="py-2 px-3">
                <span class="inline-flex items-center px-1.5 py-0.5 rounded-input text-xs font-medium" :class="ACTION_STYLE(log.action)">
                  {{ log.action }}
                </span>
              </td>
              <td class="py-2 px-3 font-mono text-xs chrome-text-normal">{{ objectLabel(log) }}</td>
              <td class="py-2 px-3">
                <span class="inline-flex items-center gap-1.5 text-xs chrome-text-normal">
                  <span class="w-2 h-2 rounded-full" :class="resultDot(log.result)" />
                  {{ log.result }}
                </span>
              </td>
              <td class="py-2 px-3 font-mono text-xs chrome-text-muted">
                <button
                  v-if="log.request_id"
                  type="button"
                  class="hover:chrome-accent transition-colors"
                  :title="t('admin.audit.copy_request_id')"
                  @click.stop="copyRequestId(log.request_id)"
                >
                  {{ log.request_id.slice(0, 8) }}
                </button>
                <span v-else>—</span>
              </td>
            </tr>
            <tr v-if="expanded.has(log.id)" class="border-b chrome-border-subtle">
              <td colspan="7" class="px-3 py-3" style="background-color: rgb(var(--bg-panel-elevated) / 0.3);">
                <div class="text-xs chrome-text-muted mb-1.5">{{ t('admin.audit.detail') }}</div>
                <pre class="text-xs font-mono chrome-text-normal whitespace-pre-wrap break-all overflow-x-auto">{{ log.detail ? JSON.stringify(log.detail, null, 2) : t('admin.audit.no_detail') }}</pre>
                <div class="text-xs chrome-text-muted mt-2 font-mono">request_id: {{ log.request_id ?? '—' }}</div>
              </td>
            </tr>
          </template>
        </tbody>
      </table>
    </div>

    <!-- 分页 -->
    <div v-if="logs.length > 0" class="flex items-center justify-between mt-4">
      <div class="text-xs chrome-text-muted">{{ t('admin.audit.page_n', { n: page + 1 }) }}</div>
      <div class="flex gap-2">
        <button type="button" class="chrome-btn-secondary" :disabled="page === 0 || query.isFetching.value" @click="page--">{{ t('common.prev_page') }}</button>
        <button type="button" class="chrome-btn-secondary" :disabled="!hasNext || query.isFetching.value" @click="page++">{{ t('common.next_page') }}</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.filter-label {
  @apply block text-xs uppercase tracking-wider font-medium;
  color: rgb(var(--text-muted));
}
</style>
