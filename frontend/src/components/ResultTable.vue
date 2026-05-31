<script setup lang="ts">
/**
 * ResultTable —— 查询结果表(SqlWorkspace / Jobs drill-down 共用)。
 *
 * Props:
 *   columns / rows  —— 后端 /jobs/{id}/result 返回的形状
 *   offset / limit / loadedRows  —— 当前分页 + 总行数(可能 null)
 *   truncated       —— 后端截断了(超过 spool 上限)
 *
 * Emits:
 *   change-page(offset)  —— 父组件 fetch 下一页
 *
 * 容量保护:渲染 ≤ 1000 行(超过给警告 + 限制),避免拖垮浏览器。
 *
 * NULL 显示:斜体 muted "NULL";空字符串显示 "''";其他原样字符串化。
 */
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { ChevronLeft, ChevronRight, AlertTriangle, Database } from 'lucide-vue-next'
import EmptyState from './EmptyState.vue'

interface Column {
  name: string
  type: string
  nullable: boolean
  primary_key: boolean
}

interface Row {
  values: unknown[]
}

const props = withDefaults(
  defineProps<{
    columns: Column[]
    rows: Row[]
    offset: number
    limit: number
    loadedRows?: number | null
    truncated?: boolean | null
  }>(),
  { loadedRows: null, truncated: null },
)

const emit = defineEmits<{
  (e: 'change-page', offset: number): void
}>()

const { t } = useI18n()

const MAX_ROWS = 1000
const tooMany = computed(() => props.rows.length > MAX_ROWS)
const displayRows = computed(() => (tooMany.value ? props.rows.slice(0, MAX_ROWS) : props.rows))

const total = computed(() => props.loadedRows ?? null)
const hasNext = computed(() => {
  if (total.value === null) return props.rows.length === props.limit
  return props.offset + props.rows.length < total.value
})
const hasPrev = computed(() => props.offset > 0)

const startRow = computed(() => (props.rows.length === 0 ? 0 : props.offset + 1))
const endRow = computed(() => props.offset + props.rows.length)

function display(v: unknown): string {
  if (v === null || v === undefined) return 'NULL'
  if (v === '') return "''"
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}
function isNull(v: unknown): boolean {
  return v === null || v === undefined
}
function isNumber(col: Column): boolean {
  const t = col.type.toLowerCase()
  return /int|num|dec|float|real|double|bigint|smallint/.test(t)
}
</script>

<template>
  <div class="flex flex-col h-full">
    <!-- 截断警告 -->
    <div
      v-if="truncated"
      class="flex items-center gap-2 px-3 py-2 text-xs border-b chrome-border"
      style="background-color: rgb(245 158 11 / 0.10); color: rgb(180 83 9);"
    >
      <AlertTriangle class="w-3.5 h-3.5 shrink-0" />
      <span>{{ t('results.truncated_hint') }}</span>
    </div>
    <!-- 浏览器渲染上限警告 -->
    <div
      v-if="tooMany"
      class="flex items-center gap-2 px-3 py-2 text-xs border-b chrome-border"
      style="background-color: rgb(239 68 68 / 0.10); color: rgb(185 28 28);"
    >
      <AlertTriangle class="w-3.5 h-3.5 shrink-0" />
      <span>{{ t('results.too_many_rows', { max: MAX_ROWS }) }}</span>
    </div>

    <!-- 空态 -->
    <div v-if="rows.length === 0" class="flex-1 grid place-items-center">
      <EmptyState
        :icon="Database"
        :title="t('results.empty_title')"
        :hint="t('results.empty_hint')"
      />
    </div>

    <!-- 表格 -->
    <div v-else class="flex-1 overflow-auto">
      <table class="w-full text-data">
        <thead
          class="sticky top-0 z-10 border-b chrome-border-subtle"
          style="background-color: rgb(var(--bg-panel-elevated));"
        >
          <tr class="text-left text-xs chrome-text-muted uppercase tracking-wider">
            <th class="font-medium py-1.5 px-3 w-10 text-right tabular-nums">#</th>
            <th
              v-for="(c, ci) in columns"
              :key="ci"
              class="font-medium py-1.5 px-3"
              :class="isNumber(c) ? 'text-right' : 'text-left'"
            >
              <div class="flex items-center gap-1">
                <span>{{ c.name }}</span>
                <span
                  v-if="c.primary_key"
                  class="text-[9px] chrome-accent font-mono"
                  :title="t('results.col_pk')"
                >
                  PK
                </span>
              </div>
              <div class="text-[10px] chrome-text-muted normal-case font-mono">
                {{ c.type }}
              </div>
            </th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="(r, ri) in displayRows"
            :key="ri"
            class="border-b chrome-border-subtle last:border-b-0 hover:chrome-bg-elevated transition-colors"
          >
            <td class="py-1 px-3 text-right tabular-nums chrome-text-muted text-xs">
              {{ offset + ri + 1 }}
            </td>
            <td
              v-for="(v, vi) in r.values"
              :key="vi"
              class="py-1 px-3 font-mono text-xs chrome-text-normal"
              :class="[
                isNumber(columns[vi]) ? 'text-right tabular-nums' : 'text-left',
                isNull(v) && 'italic chrome-text-muted',
              ]"
            >
              {{ display(v) }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 分页 footer -->
    <div
      class="flex items-center justify-between px-3 py-2 border-t chrome-border-subtle text-xs chrome-text-muted"
    >
      <span class="tabular-nums">
        {{ t('results.page_info', {
          start: startRow,
          end: endRow,
          total: total ?? '?',
        }) }}
      </span>
      <div class="flex items-center gap-1">
        <button
          type="button"
          class="chrome-btn-ghost"
          :disabled="!hasPrev"
          @click="emit('change-page', Math.max(0, offset - limit))"
          :title="t('common.prev_page')"
        >
          <ChevronLeft class="w-3.5 h-3.5" />
        </button>
        <button
          type="button"
          class="chrome-btn-ghost"
          :disabled="!hasNext"
          @click="emit('change-page', offset + limit)"
          :title="t('common.next_page')"
        >
          <ChevronRight class="w-3.5 h-3.5" />
        </button>
      </div>
    </div>
  </div>
</template>
