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
import type { Column, ColumnType } from '../api/types'

interface Row {
  values: unknown[]
}

// ─── ColumnType → 展示语义(对齐 / 染色 / 标记)──────────────────────
// 后端 Column.type 是 11 值统一枚举(契约 §3.2),前端按枚举一套规则染色,
// 不再 substring 匹配 driver 字符串。driver_type 原文进 tooltip。
const NUMERIC_TYPES: ReadonlySet<ColumnType> = new Set<ColumnType>([
  'integer',
  'float',
  'decimal',
])
// 列头类型标签的色系(语义色,不接 chrome accent —— 跟 variant 无关)。
const TYPE_LABEL_CLASS: Record<ColumnType, string> = {
  integer: 'text-sky-600 dark:text-sky-400',
  float: 'text-sky-600 dark:text-sky-400',
  decimal: 'text-sky-600 dark:text-sky-400',
  datetime: 'text-violet-600 dark:text-violet-400',
  date: 'text-violet-600 dark:text-violet-400',
  time: 'text-violet-600 dark:text-violet-400',
  boolean: 'text-emerald-600 dark:text-emerald-400',
  bytes: 'text-amber-600 dark:text-amber-400',
  json: 'text-amber-600 dark:text-amber-400',
  string: 'chrome-text-muted',
  unknown: 'chrome-text-muted',
}
// bytes / json 列头额外标记(短 badge),让二进制 / 结构化列一眼可辨。
const TYPE_MARKER: Partial<Record<ColumnType, string>> = {
  bytes: 'BYTES',
  json: 'JSON',
}

function isNumber(col: Column): boolean {
  return NUMERIC_TYPES.has(col.type)
}
// datetime/date/time 单元格用独立色,跟普通字符串区分(DBA 一眼挑出时间列)。
function isTemporal(col: Column): boolean {
  return col.type === 'datetime' || col.type === 'date' || col.type === 'time'
}
function typeLabelClass(col: Column): string {
  return TYPE_LABEL_CLASS[col.type] ?? 'chrome-text-muted'
}
function typeMarker(col: Column): string | undefined {
  return TYPE_MARKER[col.type]
}
// 列头副行展示:有 driver_type 显原文,否则回退到统一枚举值。
function typeText(col: Column): string {
  return col.driver_type || col.type
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
                <!-- bytes / json 列额外标记 -->
                <span
                  v-if="typeMarker(c)"
                  class="text-[8px] font-mono px-1 rounded border border-amber-300/60 text-amber-600 dark:border-amber-500/40 dark:text-amber-400"
                >
                  {{ typeMarker(c) }}
                </span>
              </div>
              <!-- 副行:driver_type 原文(tooltip 同样显原文 + 统一枚举);按枚举染色 -->
              <div
                class="text-[10px] normal-case font-mono truncate max-w-[14rem]"
                :class="typeLabelClass(c)"
                :title="`${typeText(c)} · ${c.type}`"
              >
                {{ typeText(c) }}
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
                isTemporal(columns[vi]) && !isNull(v) && 'text-violet-600 dark:text-violet-400',
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
