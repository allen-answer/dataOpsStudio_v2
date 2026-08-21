<script setup lang="ts">
/**
 * ResultTable —— 查询结果表(SqlWorkspace / Jobs drill-down 共用)。
 *
 * Props:
 *   columns / rows  —— 后端 /jobs/{id}/result 返回的形状
 *   offset / limit / loadedRows / totalRows  —— 当前分页 + 总行数(可能 null)
 *   truncated       —— 后端截断了(超过 spool 上限)
 *
 * Emits:
 *   change-page(offset)  —— 父组件 fetch 下一页
 *
 * 容量保护:渲染 ≤ 1000 行(超过给警告 + 限制),避免拖垮浏览器。
 *
 * NULL 显示:斜体 muted "NULL";空字符串显示 "''";其他原样字符串化。
 */
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ChevronLeft, ChevronRight, AlertTriangle, Database, Info } from 'lucide-vue-next'
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

const props = withDefaults(
  defineProps<{
    columns: Column[]
    rows: Row[]
    offset: number
    limit: number
    loadedRows?: number | null
    totalRows?: number | null
    truncated?: boolean | null
    hasMore?: boolean | null
    paginationMode?: 'ordered_offset' | 'unavailable' | null
    paginationReason?: string | null
    previewTruncatedCells?: number
    maxResultRows?: number | null
    loading?: boolean
  }>(),
  {
    loadedRows: null,
    totalRows: null,
    truncated: null,
    hasMore: null,
    paginationMode: null,
    paginationReason: null,
    previewTruncatedCells: 0,
    maxResultRows: null,
    loading: false,
  },
)

const emit = defineEmits<{
  (e: 'change-page', offset: number): void
}>()

const { t } = useI18n()

const MAX_ROWS = 1000
const ROW_HEIGHT = 28
const OVERSCAN = 8
const DEFAULT_VIEWPORT_HEIGHT = 320
const tooMany = computed(() => props.rows.length > MAX_ROWS)
const displayRows = computed(() => (tooMany.value ? props.rows.slice(0, MAX_ROWS) : props.rows))

interface ColumnMeta {
  column: Column
  isNumber: boolean
  isTemporal: boolean
  typeLabelClass: string
  typeMarker: string | undefined
  typeText: string
}

interface RenderedCell {
  text: string
  className: string
}

interface VirtualRow {
  index: number
  cells: RenderedCell[]
}

const scrollContainer = ref<HTMLElement | null>(null)
const scrollTop = ref(0)
const viewportHeight = ref(0)
let resizeObserver: ResizeObserver | null = null

// Column semantics are stable for a result page; compute them once rather
// than scanning the numeric/temporal sets from every cell in the template.
const columnMeta = computed<ColumnMeta[]>(() =>
  props.columns.map((column) => ({
    column,
    isNumber: NUMERIC_TYPES.has(column.type),
    isTemporal: column.type === 'datetime' || column.type === 'date' || column.type === 'time',
    typeLabelClass: TYPE_LABEL_CLASS[column.type] ?? 'chrome-text-muted',
    typeMarker: TYPE_MARKER[column.type],
    typeText: column.driver_type || column.type,
  })),
)

const visibleRange = computed(() => {
  const rowCount = displayRows.value.length
  const viewport = viewportHeight.value || DEFAULT_VIEWPORT_HEIGHT
  const first = Math.min(
    rowCount,
    Math.max(0, Math.floor(scrollTop.value / ROW_HEIGHT) - OVERSCAN),
  )
  const visibleCount = Math.ceil(viewport / ROW_HEIGHT) + OVERSCAN * 2 + 1
  return { start: first, end: Math.min(rowCount, first + visibleCount) }
})

const topSpacerHeight = computed(() => visibleRange.value.start * ROW_HEIGHT)
const bottomSpacerHeight = computed(() =>
  Math.max(0, (displayRows.value.length - visibleRange.value.end) * ROW_HEIGHT),
)
const ariaRowCount = computed(() => {
  if (props.totalRows !== null) return props.totalRows + 1
  if (props.hasMore === true) return -1
  const totalRows = props.loadedRows ?? props.offset + props.rows.length
  return totalRows + 1 // Include the sticky header row in the table row count.
})
const ariaColCount = computed(() => props.columns.length + 1) // Include the row-number column.

const totalLabel = computed(() => {
  if (props.totalRows !== null) return String(props.totalRows)
  if (props.loadedRows === null) return '?'
  return props.hasMore ? `${props.loadedRows}+` : String(props.loadedRows)
})
const hasNext = computed(() => {
  if (props.loading) return false
  if (props.loadedRows !== null && props.offset + props.rows.length < props.loadedRows) return true
  if (props.loadedRows !== null && props.maxResultRows !== null) {
    if (props.loadedRows >= props.maxResultRows) return false
  }
  return Boolean(props.hasMore && props.paginationMode === 'ordered_offset')
})
const hasPrev = computed(() => !props.loading && props.offset > 0)

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

function cellClass(meta: ColumnMeta | undefined, value: unknown): string {
  const nullValue = isNull(value)
  return [
    meta?.isNumber ? 'text-right tabular-nums' : 'text-left',
    meta?.isTemporal && !nullValue && 'text-violet-600 dark:text-violet-400',
    nullValue && 'italic chrome-text-muted',
  ]
    .filter(Boolean)
    .join(' ')
}

const virtualRows = computed<VirtualRow[]>(() => {
  const { start, end } = visibleRange.value
  return displayRows.value.slice(start, end).map((row, index) => ({
    index: start + index,
    cells: row.values.map((value, valueIndex) => ({
      text: display(value),
      className: cellClass(columnMeta.value[valueIndex], value),
    })),
  }))
})

function onScroll(event: Event): void {
  const target = event.currentTarget as HTMLElement
  scrollTop.value = target.scrollTop
  viewportHeight.value = target.clientHeight
}

function syncViewport(target: HTMLElement | null = scrollContainer.value): void {
  viewportHeight.value = target?.clientHeight ?? 0
}

function resetScrollPosition(): void {
  scrollTop.value = 0
  const target = scrollContainer.value
  if (!target) {
    viewportHeight.value = 0
    return
  }
  target.scrollTop = 0
  syncViewport(target)
}

function observeScrollContainer(target: HTMLElement | null): void {
  resizeObserver?.disconnect()
  resizeObserver = null
  resetScrollPosition()
  if (!target || typeof ResizeObserver === 'undefined') return
  resizeObserver = new ResizeObserver(() => syncViewport(target))
  resizeObserver.observe(target)
}

watch(scrollContainer, (target) => observeScrollContainer(target), { flush: 'post' })

watch(
  [() => props.offset, () => props.columns],
  () => {
    void nextTick(resetScrollPosition)
  },
  { flush: 'post' },
)

onUnmounted(() => {
  resizeObserver?.disconnect()
  resizeObserver = null
})

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
    <div
      v-if="hasMore && paginationMode === 'unavailable'"
      class="flex items-center gap-2 px-3 py-2 text-xs border-b chrome-border"
      style="background-color: rgb(245 158 11 / 0.10); color: rgb(180 83 9);"
    >
      <AlertTriangle class="w-3.5 h-3.5 shrink-0" />
      <span>{{ t('results.pagination_order_required') }}</span>
    </div>
    <div
      v-else-if="hasMore && paginationMode === 'ordered_offset'"
      class="flex items-center gap-2 px-3 py-2 text-xs border-b chrome-border chrome-text-muted"
    >
      <Info class="w-3.5 h-3.5 shrink-0" />
      <span>{{ t('results.pagination_fresh_read') }}</span>
    </div>
    <div
      v-if="previewTruncatedCells > 0"
      class="flex items-center gap-2 px-3 py-2 text-xs border-b chrome-border chrome-text-muted"
    >
      <Info class="w-3.5 h-3.5 shrink-0" />
      <span>{{ t('results.preview_truncated', { count: previewTruncatedCells }) }}</span>
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
    <div v-else ref="scrollContainer" class="flex-1 overflow-auto" data-testid="result-table-scroll" @scroll.passive="onScroll">
      <table
        class="w-full text-data"
        :aria-rowcount="ariaRowCount"
        :aria-colcount="ariaColCount"
      >
        <thead
          class="sticky top-0 z-10 border-b chrome-border-subtle"
          style="background-color: rgb(var(--bg-panel-elevated));"
        >
          <tr aria-rowindex="1" class="text-left text-xs chrome-text-muted uppercase tracking-wider">
            <th class="font-medium py-1.5 px-3 w-10 text-right tabular-nums">#</th>
            <th
              v-for="(meta, ci) in columnMeta"
              :key="ci"
              class="font-medium py-1.5 px-3"
              :class="meta.isNumber ? 'text-right' : 'text-left'"
            >
              <div class="flex items-center gap-1">
                <span>{{ meta.column.name }}</span>
                <span
                  v-if="meta.column.primary_key"
                  class="text-[9px] chrome-accent font-mono"
                  :title="t('results.col_pk')"
                >
                  PK
                </span>
                <!-- bytes / json 列额外标记 -->
                <span
                  v-if="meta.typeMarker"
                  class="text-[8px] font-mono px-1 rounded border border-amber-300/60 text-amber-600 dark:border-amber-500/40 dark:text-amber-400"
                >
                  {{ meta.typeMarker }}
                </span>
              </div>
              <!-- 副行:driver_type 原文(tooltip 同样显原文 + 统一枚举);按枚举染色 -->
              <div
                class="text-[10px] normal-case font-mono truncate max-w-[14rem]"
                :class="meta.typeLabelClass"
                :title="`${meta.typeText} · ${meta.column.type}`"
              >
                {{ meta.typeText }}
              </div>
            </th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-if="topSpacerHeight > 0"
            aria-hidden="true"
            data-testid="result-table-top-spacer"
          >
            <td
              :colspan="columns.length + 1"
              :style="{ height: `${topSpacerHeight}px`, padding: 0, border: 0 }"
            />
          </tr>
          <tr
            v-for="item in virtualRows"
            :key="item.index"
            :data-row-index="offset + item.index"
            :aria-rowindex="offset + item.index + 2"
            :style="{ height: `${ROW_HEIGHT}px` }"
            class="border-b chrome-border-subtle last:border-b-0 hover:chrome-bg-elevated transition-colors whitespace-nowrap"
          >
            <td class="py-1 px-3 text-right tabular-nums chrome-text-muted text-xs">
              {{ offset + item.index + 1 }}
            </td>
            <td
              v-for="(cell, vi) in item.cells"
              :key="vi"
              class="py-1 px-3 font-mono text-xs chrome-text-normal max-w-[32rem] overflow-hidden text-ellipsis whitespace-nowrap"
              :class="cell.className"
            >
              {{ cell.text }}
            </td>
          </tr>
          <tr
            v-if="bottomSpacerHeight > 0"
            aria-hidden="true"
            data-testid="result-table-bottom-spacer"
          >
            <td
              :colspan="columns.length + 1"
              :style="{ height: `${bottomSpacerHeight}px`, padding: 0, border: 0 }"
            />
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
          total: totalLabel,
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
