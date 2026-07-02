<script setup lang="ts">
/**
 * LineageView —— /projects/:id/lineage(2.3.0 血缘,tech-design §2.4 + ADR-0019)
 *
 * 三个 tab:
 *  - 子图查询(主视图):焦点表/列 N 跳邻域子图,自定义 SVG 分层布局
 *    (焦点居中,上游在左 / 下游在右,按 depth 分列)。★ 不做全景图(ADR-0019)。
 *  - 影响分析:焦点表 → 下游波及清单,按 depth 分组(纯图遍历,不依赖 AI)。
 *  - SQL 解析:选数据源 + 粘贴 SQL → analyze 端点落边;支持 refresh 绕过 sql_hash 缓存。
 *
 * 字段全部锚 api/lineage.ts(锚后端 schemas.py + core.py),不臆造。
 */
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useQuery } from '@tanstack/vue-query'
import {
  AlertTriangle,
  FileSearch,
  Network,
  Play,
  RefreshCw,
  Target,
  Waypoints,
} from 'lucide-vue-next'
import { listDatasources } from '../api/datasources'
import {
  analyzeLineage,
  getLineageImpact,
  getLineageSubgraph,
  type LineageAnalyzeResponse,
  type LineageDirection,
  type LineageImpactItem,
  type LineageImpactResponse,
  type LineageSubgraphEdge,
  type LineageSubgraphNode,
  type LineageSubgraphResponse,
} from '../api/lineage'
import { ApiError, type DatasourceListItem } from '../api/types'
import LoadingDots from '../components/LoadingDots.vue'

type Tab = 'subgraph' | 'impact' | 'analyze'

// 血缘解析器支持的方言(app/domain/lineage/parser.py _normalize_dialect)
const LINEAGE_DIALECTS: ReadonlySet<string> = new Set(['mysql', 'oracle', 'dm'])

// ── SVG 布局常量 ────────────────────────────────────────────────────
const NODE_W = 176
const NODE_H = 34
const COL_W = 252
const ROW_H = 54
const PAD = 28

const { t } = useI18n()
const route = useRoute()
const projectId = computed(() => (typeof route.params.id === 'string' ? route.params.id : ''))

const tab = ref<Tab>('subgraph')

// ── datasources(analyze tab 用)─────────────────────────────────────
const dsQuery = useQuery({
  queryKey: computed(() => ['datasources', projectId.value]),
  queryFn: () => listDatasources(projectId.value),
  enabled: computed(() => Boolean(projectId.value)),
})
const datasources = computed<DatasourceListItem[]>(() => dsQuery.data.value ?? [])

// ── 子图查询 ────────────────────────────────────────────────────────
const subgraphFocus = ref('')
const subgraphDirection = ref<LineageDirection>('downstream')
const subgraphDepth = ref(3) // 后端默认 3,上限 5(_LINEAGE_DEPTH_QUERY ge=1 le=5)
const includeColumns = ref(false)
const subgraphLoading = ref(false)
const subgraphError = ref<string | null>(null)
const subgraphData = ref<LineageSubgraphResponse | null>(null)

async function runSubgraph(): Promise<void> {
  const focus = subgraphFocus.value.trim()
  if (!projectId.value || !focus) {
    subgraphError.value = t('lineage.error_focus_required')
    return
  }
  subgraphLoading.value = true
  subgraphError.value = null
  try {
    subgraphData.value = await getLineageSubgraph(projectId.value, {
      focus,
      direction: subgraphDirection.value,
      maxDepth: subgraphDepth.value,
      includeColumns: includeColumns.value,
    })
  } catch (e) {
    subgraphError.value = errorMessage(e)
  } finally {
    subgraphLoading.value = false
  }
}

/** 点击节点 → 设为新焦点重查(列节点焦点用 "table.column" 全 id)。 */
function onNodeClick(node: LineageSubgraphNode): void {
  subgraphFocus.value = node.id
  void runSubgraph()
}

const depthChips = computed<[string, number][]>(() =>
  Object.entries(subgraphData.value?.depth_counts ?? {}).sort(
    (a, b) => Number(a[0]) - Number(b[0]),
  ),
)

// ── SVG 分层布局 ────────────────────────────────────────────────────
interface LaidNode {
  node: LineageSubgraphNode
  x: number
  y: number
}
interface LaidEdge {
  edge: LineageSubgraphEdge
  d: string
}
interface GraphLayout {
  nodes: LaidNode[]
  edges: LaidEdge[]
  width: number
  height: number
}

/**
 * 分层布局:焦点列 0;节点列 = depth × 方向符号(上游 -1 / 下游 +1)。
 * 方向符号来自触及该节点的边的 direction(上/下游 CTE 各自只产出本侧节点)。
 * 只对后端返回的焦点子图排版 —— 不做全图计算(ADR-0019)。
 */
const graphLayout = computed<GraphLayout | null>(() => {
  const data = subgraphData.value
  if (!data || data.nodes.length === 0) return null

  const side = new Map<string, number>([[data.focus, 0]])
  for (const edge of data.edges) {
    const sign = edge.direction === 'upstream' ? -1 : 1
    if (!side.has(edge.source)) side.set(edge.source, sign)
    if (!side.has(edge.target)) side.set(edge.target, sign)
  }
  const colOf = (node: LineageSubgraphNode): number => node.depth * (side.get(node.id) ?? 1)

  const byCol = new Map<number, LineageSubgraphNode[]>()
  for (const node of data.nodes) {
    const col = colOf(node)
    const list = byCol.get(col)
    if (list) list.push(node)
    else byCol.set(col, [node])
  }
  const colKeys = [...byCol.keys()]
  const minCol = Math.min(...colKeys)
  const maxCol = Math.max(...colKeys)
  const maxRows = Math.max(...[...byCol.values()].map((list) => list.length))

  const pos = new Map<string, { x: number; y: number }>()
  for (const [col, list] of byCol) {
    list.sort((a, b) => a.id.localeCompare(b.id))
    const offsetY = ((maxRows - list.length) * ROW_H) / 2
    list.forEach((node, i) => {
      pos.set(node.id, { x: PAD + (col - minCol) * COL_W, y: PAD + offsetY + i * ROW_H })
    })
  }

  const laidNodes: LaidNode[] = data.nodes.map((node) => {
    const p = pos.get(node.id) ?? { x: PAD, y: PAD }
    return { node, x: p.x, y: p.y }
  })
  const laidEdges: LaidEdge[] = []
  for (const edge of data.edges) {
    const s = pos.get(edge.source)
    const e = pos.get(edge.target)
    if (!s || !e) continue
    const x1 = s.x + NODE_W
    const y1 = s.y + NODE_H / 2
    const x2 = e.x
    const y2 = e.y + NODE_H / 2
    const dx = Math.max(40, Math.abs(x2 - x1) / 2)
    laidEdges.push({
      edge,
      d: `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`,
    })
  }
  return {
    nodes: laidNodes,
    edges: laidEdges,
    width: PAD * 2 + (maxCol - minCol) * COL_W + NODE_W,
    height: PAD * 2 + (maxRows - 1) * ROW_H + NODE_H,
  }
})

function nodeText(node: LineageSubgraphNode): string {
  const full = node.column ? `${node.table}.${node.column}` : node.table
  return full.length > 24 ? `${full.slice(0, 23)}…` : full
}

function edgeTitle(edge: LineageSubgraphEdge): string {
  const parts = [`${edge.source} → ${edge.target}`]
  if (edge.transformation) {
    parts.push(
      edge.transformation_subtype && edge.transformation_subtype !== edge.transformation
        ? `${edge.transformation}/${edge.transformation_subtype}`
        : edge.transformation,
    )
  }
  if (edge.inferred) parts.push(`inferred (${edge.inference_status}, ${edge.confidence})`)
  return parts.join(' · ')
}

// ── 影响分析 ────────────────────────────────────────────────────────
const impactFocus = ref('')
const impactDepth = ref(3)
const impactLoading = ref(false)
const impactError = ref<string | null>(null)
const impactData = ref<LineageImpactResponse | null>(null)

async function runImpact(): Promise<void> {
  const focus = impactFocus.value.trim()
  if (!projectId.value || !focus) {
    impactError.value = t('lineage.error_focus_required')
    return
  }
  impactLoading.value = true
  impactError.value = null
  try {
    impactData.value = await getLineageImpact(projectId.value, focus, impactDepth.value)
  } catch (e) {
    impactError.value = errorMessage(e)
  } finally {
    impactLoading.value = false
  }
}

const impactGroups = computed<[number, LineageImpactItem[]][]>(() => {
  const map = new Map<number, LineageImpactItem[]>()
  for (const item of impactData.value?.impacts ?? []) {
    const list = map.get(item.depth)
    if (list) list.push(item)
    else map.set(item.depth, [item])
  }
  return [...map.entries()].sort((a, b) => a[0] - b[0])
})

// ── SQL 解析(analyze)──────────────────────────────────────────────
const analyzeDsId = ref('')
const analyzeSourceRef = ref('')
const analyzeDefaultSchema = ref('')
const analyzeSql = ref('')
const analyzeRefresh = ref(false)
const analyzing = ref(false)
const analyzeError = ref<string | null>(null)
const analyzeResult = ref<LineageAnalyzeResponse | null>(null)

watch(datasources, (list) => {
  if (!analyzeDsId.value && list.length > 0) analyzeDsId.value = list[0].id
})

// dialect 跟随数据源 db_type(请求不带 dialect,由后端从 datasource 推导)
const analyzeDialect = computed(
  () => datasources.value.find((ds) => ds.id === analyzeDsId.value)?.db_type ?? '',
)
const dialectUnsupported = computed(
  () => Boolean(analyzeDialect.value) && !LINEAGE_DIALECTS.has(analyzeDialect.value),
)

async function onAnalyze(): Promise<void> {
  if (!analyzeDsId.value || !analyzeSourceRef.value.trim() || !analyzeSql.value.trim()) {
    analyzeError.value = t('lineage.analyze_required')
    return
  }
  analyzing.value = true
  analyzeError.value = null
  try {
    analyzeResult.value = await analyzeLineage(
      projectId.value,
      {
        datasource_id: analyzeDsId.value,
        sql_text: analyzeSql.value,
        source_ref: analyzeSourceRef.value.trim(),
        default_schema: analyzeDefaultSchema.value.trim() || null,
      },
      analyzeRefresh.value,
    )
  } catch (e) {
    if (e instanceof ApiError && e.code === 'lineage_parse_failed') {
      analyzeError.value = t('lineage.error_parse_failed')
    } else {
      analyzeError.value = errorMessage(e)
    }
  } finally {
    analyzing.value = false
  }
}

/**
 * parse_errors 明细面板:后端当前 parse_summary 只含 parse_error_count 等计数
 * (core.py 存 LineageReport.report 摘要);若后续把逐条 parse_errors 放进
 * parse_summary,这里宽容读取并逐条渲染(error_type / statement_index /
 * statement_type / message,锚 app/domain/lineage/models.py LineageParseError)。
 */
interface ParseErrorEntry {
  statement_index?: number
  error_type?: string
  message?: string
  statement_type?: string | null
  unsupported?: boolean
}
const parseErrors = computed<ParseErrorEntry[]>(() => {
  const raw = analyzeResult.value?.parse_summary?.parse_errors
  return Array.isArray(raw) ? (raw as ParseErrorEntry[]) : []
})
const parseErrorCount = computed(
  () => Number(analyzeResult.value?.parse_summary?.parse_error_count ?? 0),
)

// ── helpers ─────────────────────────────────────────────────────────
function errorMessage(e: unknown): string {
  if (e instanceof ApiError) return e.message || t('common.error_unknown')
  return t('common.error_unknown')
}
</script>

<template>
  <div class="flex h-full min-h-0 flex-col chrome-bg-main">
    <!-- 顶部:标题 + tab(.chrome-tab,图标+文字)-->
    <div class="flex items-center gap-1 px-3 py-2 border-b chrome-border-subtle chrome-bg-panel">
      <div class="flex items-center gap-2 pr-3 text-section font-semibold chrome-text-heading">
        <Network class="w-4 h-4" />
        {{ t('lineage.workspace') }}
        <span class="text-xs font-normal chrome-text-muted">/ {{ projectId.slice(0, 8) }}</span>
      </div>
      <button
        type="button"
        class="chrome-tab"
        :class="tab === 'subgraph' && 'chrome-accent-light-bg chrome-accent'"
        @click="tab = 'subgraph'"
      >
        <Waypoints class="w-4 h-4" /> {{ t('lineage.tab_subgraph') }}
      </button>
      <button
        type="button"
        class="chrome-tab"
        :class="tab === 'impact' && 'chrome-accent-light-bg chrome-accent'"
        @click="tab = 'impact'"
      >
        <Target class="w-4 h-4" /> {{ t('lineage.tab_impact') }}
      </button>
      <button
        type="button"
        class="chrome-tab"
        :class="tab === 'analyze' && 'chrome-accent-light-bg chrome-accent'"
        @click="tab = 'analyze'"
      >
        <FileSearch class="w-4 h-4" /> {{ t('lineage.tab_analyze') }}
      </button>
    </div>

    <div class="flex-1 min-h-0 overflow-auto">
      <!-- ============ 子图查询 tab(主视图)============ -->
      <div v-show="tab === 'subgraph'" class="p-4 space-y-3">
        <div class="flex flex-wrap items-end gap-3">
          <label class="block flex-1 min-w-[16rem]">
            <span class="block text-xs chrome-text-muted mb-1">{{ t('lineage.focus') }}</span>
            <input
              v-model="subgraphFocus"
              class="chrome-input w-full font-mono text-sm"
              :placeholder="t('lineage.focus_ph')"
              @keydown.enter="runSubgraph"
            />
          </label>
          <label class="block">
            <span class="block text-xs chrome-text-muted mb-1">{{ t('lineage.direction') }}</span>
            <select v-model="subgraphDirection" class="chrome-input text-sm">
              <option value="upstream">{{ t('lineage.direction_upstream') }}</option>
              <option value="downstream">{{ t('lineage.direction_downstream') }}</option>
              <option value="both">{{ t('lineage.direction_both') }}</option>
            </select>
          </label>
          <label class="block">
            <span class="block text-xs chrome-text-muted mb-1">{{ t('lineage.max_depth') }}</span>
            <input
              v-model.number="subgraphDepth"
              type="number"
              min="1"
              max="5"
              class="chrome-input w-20 text-sm"
            />
          </label>
          <label class="flex items-center gap-2 text-xs chrome-text-muted pb-2">
            <input v-model="includeColumns" type="checkbox" />
            {{ t('lineage.include_columns') }}
          </label>
          <button
            type="button"
            class="chrome-btn-primary text-sm"
            :disabled="subgraphLoading"
            @click="runSubgraph"
          >
            <Play class="w-3.5 h-3.5" />
            {{ subgraphLoading ? t('lineage.querying') : t('lineage.query') }}
          </button>
        </div>

        <div v-if="subgraphError" class="text-xs text-red-600 dark:text-red-400">
          {{ subgraphError }}
        </div>

        <!-- 空闲引导(还没查过)-->
        <div
          v-if="!subgraphData && !subgraphLoading"
          class="rounded-card border chrome-border chrome-bg-panel p-6 max-w-xl"
        >
          <div class="flex items-center gap-2 text-sm font-medium chrome-text-heading">
            <Waypoints class="w-4 h-4" /> {{ t('lineage.subgraph_idle_title') }}
          </div>
          <p class="mt-2 text-xs chrome-text-muted">{{ t('lineage.subgraph_idle_hint') }}</p>
          <button
            type="button"
            class="chrome-btn-secondary text-xs mt-3"
            @click="tab = 'analyze'"
          >
            <FileSearch class="w-3.5 h-3.5" /> {{ t('lineage.go_analyze') }}
          </button>
        </div>

        <div v-if="subgraphLoading" class="chrome-text-muted text-sm"><LoadingDots /></div>

        <template v-if="subgraphData && !subgraphLoading">
          <!-- 摘要:节点/边计数 + depth 分层 + 截断提示 -->
          <div class="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] chrome-text-muted">
            <span class="font-mono chrome-text-heading">{{ subgraphData.focus }}</span>
            <span>{{ t('lineage.node_count', { count: subgraphData.node_count }) }}</span>
            <span>{{ t('lineage.edge_count', { count: subgraphData.edge_count }) }}</span>
            <span v-for="[depth, count] in depthChips" :key="depth">
              {{ t('lineage.depth_chip', { depth, count }) }}
            </span>
          </div>
          <div
            v-if="subgraphData.truncated"
            class="flex items-center gap-2 rounded-card border border-amber-300 dark:border-amber-500/40 bg-amber-50 dark:bg-amber-500/10 p-2 text-xs text-amber-800 dark:text-amber-200"
          >
            <AlertTriangle class="w-4 h-4 shrink-0" /> {{ t('lineage.truncated_hint') }}
          </div>

          <!-- 空态:焦点没有任何边 → 引导先 analyze -->
          <div
            v-if="subgraphData.edge_count === 0"
            class="rounded-card border chrome-border chrome-bg-panel p-6 max-w-xl"
          >
            <div class="flex items-center gap-2 text-sm font-medium chrome-text-heading">
              <AlertTriangle class="w-4 h-4" /> {{ t('lineage.subgraph_empty_title') }}
            </div>
            <p class="mt-2 text-xs chrome-text-muted">{{ t('lineage.subgraph_empty_hint') }}</p>
            <button
              type="button"
              class="chrome-btn-secondary text-xs mt-3"
              @click="tab = 'analyze'"
            >
              <FileSearch class="w-3.5 h-3.5" /> {{ t('lineage.go_analyze') }}
            </button>
          </div>

          <!-- 子图 SVG(按 depth 分列,焦点列高亮;点节点切焦点)-->
          <template v-else-if="graphLayout">
            <div
              class="rounded-card border chrome-border chrome-bg-panel overflow-auto"
              style="max-height: 62vh"
            >
              <svg :width="graphLayout.width" :height="graphLayout.height" role="img">
                <defs>
                  <marker
                    id="lineage-arrow"
                    viewBox="0 0 8 8"
                    refX="7"
                    refY="4"
                    markerWidth="6"
                    markerHeight="6"
                    orient="auto-start-reverse"
                  >
                    <path d="M 0 0 L 8 4 L 0 8 z" class="lineage-arrow" />
                  </marker>
                </defs>
                <path
                  v-for="le in graphLayout.edges"
                  :key="`${le.edge.id}-${le.edge.direction}`"
                  :d="le.d"
                  fill="none"
                  marker-end="url(#lineage-arrow)"
                  class="lineage-edge"
                  :class="{
                    'lineage-edge-column': le.edge.edge_kind === 'column',
                    'lineage-edge-inferred': le.edge.inferred,
                  }"
                >
                  <title>{{ edgeTitle(le.edge) }}</title>
                </path>
                <g
                  v-for="ln in graphLayout.nodes"
                  :key="ln.node.id"
                  class="lineage-node"
                  :transform="`translate(${ln.x}, ${ln.y})`"
                  @click="onNodeClick(ln.node)"
                >
                  <rect
                    :width="NODE_W"
                    :height="NODE_H"
                    rx="6"
                    class="lineage-node-rect"
                    :class="{
                      'lineage-node-rect-focus': ln.node.id === subgraphData.focus,
                      'lineage-node-rect-column': ln.node.kind === 'column',
                    }"
                  />
                  <text :x="10" :y="NODE_H / 2 + 4" class="lineage-node-text">
                    {{ nodeText(ln.node) }}
                  </text>
                  <title>{{ ln.node.id }}</title>
                </g>
              </svg>
            </div>
            <!-- 图例 + 交互提示 -->
            <div class="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] chrome-text-muted">
              <span class="inline-flex items-center gap-1.5">
                <span class="w-3 h-3 rounded-sm border-2 lineage-legend-focus" />
                {{ t('lineage.legend_focus') }}
              </span>
              <span class="inline-flex items-center gap-1.5">
                <span class="w-3 h-3 rounded-sm border chrome-border chrome-bg-elevated inline-block" />
                {{ t('lineage.legend_table') }}
              </span>
              <span v-if="includeColumns" class="inline-flex items-center gap-1.5">
                <span class="w-3 h-3 rounded-sm border chrome-border inline-block" />
                {{ t('lineage.legend_column') }}
              </span>
              <span class="inline-flex items-center gap-1.5">
                <span class="w-4 border-t-2 border-dashed border-amber-500 inline-block" />
                {{ t('lineage.legend_inferred') }}
              </span>
              <span>{{ t('lineage.node_click_hint') }}</span>
            </div>
          </template>
        </template>
      </div>

      <!-- ============ 影响分析 tab ============ -->
      <div v-show="tab === 'impact'" class="p-4 space-y-3 max-w-4xl">
        <div class="flex flex-wrap items-end gap-3">
          <label class="block flex-1 min-w-[16rem]">
            <span class="block text-xs chrome-text-muted mb-1">{{ t('lineage.impact_focus') }}</span>
            <input
              v-model="impactFocus"
              class="chrome-input w-full font-mono text-sm"
              :placeholder="t('lineage.focus_ph')"
              @keydown.enter="runImpact"
            />
          </label>
          <label class="block">
            <span class="block text-xs chrome-text-muted mb-1">{{ t('lineage.max_depth') }}</span>
            <input
              v-model.number="impactDepth"
              type="number"
              min="1"
              max="5"
              class="chrome-input w-20 text-sm"
            />
          </label>
          <button
            type="button"
            class="chrome-btn-primary text-sm"
            :disabled="impactLoading"
            @click="runImpact"
          >
            <Target class="w-3.5 h-3.5" />
            {{ impactLoading ? t('lineage.impact_running') : t('lineage.impact_run') }}
          </button>
        </div>

        <div v-if="impactError" class="text-xs text-red-600 dark:text-red-400">{{ impactError }}</div>
        <div v-if="impactLoading" class="chrome-text-muted text-sm"><LoadingDots /></div>

        <p v-if="!impactData && !impactLoading" class="text-xs chrome-text-muted max-w-xl">
          {{ t('lineage.impact_idle_hint') }}
        </p>

        <template v-if="impactData && !impactLoading">
          <div class="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] chrome-text-muted">
            <span class="font-mono chrome-text-heading">{{ impactData.focus }}</span>
            <span>{{ t('lineage.impact_count', { count: impactData.impact_count }) }}</span>
          </div>
          <div
            v-if="impactData.truncated"
            class="flex items-center gap-2 rounded-card border border-amber-300 dark:border-amber-500/40 bg-amber-50 dark:bg-amber-500/10 p-2 text-xs text-amber-800 dark:text-amber-200"
          >
            <AlertTriangle class="w-4 h-4 shrink-0" /> {{ t('lineage.truncated_hint') }}
          </div>

          <div v-if="impactData.impacts.length === 0" class="text-sm chrome-text-muted">
            {{ t('lineage.impact_empty') }}
          </div>
          <div v-for="[depth, items] in impactGroups" :key="depth" class="space-y-1">
            <div class="text-xs font-medium chrome-text-heading">
              {{ t('lineage.impact_depth_group', { depth, count: items.length }) }}
            </div>
            <div
              v-for="item in items"
              :key="item.node"
              class="rounded-card border chrome-border px-3 py-2"
            >
              <div class="text-sm font-mono chrome-text-heading">
                {{ item.table }}<span v-if="item.column" class="chrome-accent">.{{ item.column }}</span>
              </div>
              <div v-if="item.paths.length > 0" class="mt-1 space-y-0.5">
                <div class="text-[10px] uppercase tracking-wider chrome-text-muted">
                  {{ t('lineage.impact_paths') }}
                </div>
                <div
                  v-for="(path, pi) in item.paths"
                  :key="pi"
                  class="text-[11px] font-mono chrome-text-muted"
                >
                  {{ path.join(' → ') }}
                </div>
              </div>
            </div>
          </div>
        </template>
      </div>

      <!-- ============ SQL 解析 tab ============ -->
      <div v-show="tab === 'analyze'" class="p-4 space-y-4 max-w-3xl">
        <div v-if="datasources.length === 0" class="text-sm chrome-text-muted">
          {{ t('lineage.no_datasource') }}
        </div>
        <template v-else>
          <div class="grid grid-cols-2 gap-4">
            <label class="block">
              <span class="block text-xs chrome-text-muted mb-1">{{ t('lineage.datasource') }}</span>
              <select v-model="analyzeDsId" class="chrome-input w-full text-sm">
                <option v-for="ds in datasources" :key="ds.id" :value="ds.id">{{ ds.name }}</option>
              </select>
              <span class="block mt-1 text-[11px] chrome-text-muted">
                {{ t('lineage.dialect_follow', { dialect: analyzeDialect }) }}
              </span>
            </label>
            <label class="block">
              <span class="block text-xs chrome-text-muted mb-1">{{ t('lineage.source_ref') }}</span>
              <input
                v-model="analyzeSourceRef"
                class="chrome-input w-full text-sm"
                maxlength="512"
                :placeholder="t('lineage.source_ref_ph')"
              />
            </label>
            <label class="block col-span-2">
              <span class="block text-xs chrome-text-muted mb-1">{{ t('lineage.default_schema') }}</span>
              <input
                v-model="analyzeDefaultSchema"
                class="chrome-input w-full text-sm font-mono"
                maxlength="128"
                :placeholder="t('lineage.default_schema_ph')"
              />
            </label>
          </div>

          <div
            v-if="dialectUnsupported"
            class="flex items-center gap-2 text-xs text-amber-700 dark:text-amber-300"
          >
            <AlertTriangle class="w-4 h-4 shrink-0" />
            {{ t('lineage.dialect_unsupported', { db: analyzeDialect }) }}
          </div>

          <label class="block">
            <span class="block text-xs chrome-text-muted mb-1">{{ t('lineage.sql_text') }}</span>
            <textarea
              v-model="analyzeSql"
              rows="10"
              class="chrome-input w-full text-sm font-mono"
              :placeholder="t('lineage.sql_ph')"
            />
          </label>

          <div class="flex items-center gap-3">
            <button
              type="button"
              class="chrome-btn-primary text-sm"
              :disabled="analyzing"
              @click="onAnalyze"
            >
              <RefreshCw class="w-4 h-4" :class="analyzing && 'animate-spin'" />
              {{ analyzing ? t('lineage.analyzing') : t('lineage.analyze') }}
            </button>
            <label class="flex items-center gap-2 text-xs chrome-text-muted">
              <input v-model="analyzeRefresh" type="checkbox" />
              {{ t('lineage.refresh_cache') }}
            </label>
          </div>
          <div v-if="analyzeError" class="text-xs text-red-600 dark:text-red-400">
            {{ analyzeError }}
          </div>

          <!-- 解析结果摘要 -->
          <div v-if="analyzeResult" class="rounded-card border chrome-border p-4 space-y-3">
            <div class="flex flex-wrap items-center gap-2 text-xs">
              <span class="font-medium chrome-text-heading">{{ t('lineage.result_title') }}</span>
              <span
                class="inline-flex items-center rounded-input px-1.5 py-0.5 text-[10px] font-medium"
                :class="
                  analyzeResult.cached
                    ? 'bg-slate-100 text-slate-600 dark:bg-slate-500/15 dark:text-slate-300'
                    : 'bg-emerald-50 text-emerald-700 dark:bg-emerald-500/15 dark:text-emerald-300'
                "
              >
                {{ analyzeResult.cached ? t('lineage.cached_badge') : t('lineage.fresh_badge') }}
              </span>
              <span class="chrome-text-muted">{{ analyzeResult.dialect }}</span>
              <span class="chrome-text-muted">
                {{ t('lineage.parser_version') }}: {{ analyzeResult.parser_version }}
              </span>
              <span class="chrome-text-muted font-mono" :title="analyzeResult.sql_hash">
                {{ t('lineage.sql_hash') }}: {{ analyzeResult.sql_hash.slice(0, 12) }}…
              </span>
            </div>

            <div class="grid grid-cols-4 gap-3 text-center">
              <div class="rounded-card chrome-bg-elevated p-2">
                <div class="text-lg font-semibold chrome-text-heading tabular-nums">
                  {{ analyzeResult.parse_summary.statement_count ?? '-' }}
                </div>
                <div class="text-[11px] chrome-text-muted">{{ t('lineage.stat_statements') }}</div>
              </div>
              <div class="rounded-card chrome-bg-elevated p-2">
                <div class="text-lg font-semibold chrome-text-heading tabular-nums">
                  {{ analyzeResult.table_edge_count }}
                </div>
                <div class="text-[11px] chrome-text-muted">{{ t('lineage.stat_table_edges') }}</div>
              </div>
              <div class="rounded-card chrome-bg-elevated p-2">
                <div class="text-lg font-semibold chrome-text-heading tabular-nums">
                  {{ analyzeResult.column_edge_count }}
                </div>
                <div class="text-[11px] chrome-text-muted">{{ t('lineage.stat_column_edges') }}</div>
              </div>
              <div class="rounded-card chrome-bg-elevated p-2">
                <div
                  class="text-lg font-semibold tabular-nums"
                  :class="
                    parseErrorCount > 0
                      ? 'text-amber-700 dark:text-amber-300'
                      : 'chrome-text-heading'
                  "
                >
                  {{ parseErrorCount }}
                </div>
                <div class="text-[11px] chrome-text-muted">{{ t('lineage.stat_parse_errors') }}</div>
              </div>
            </div>

            <!-- parse_errors 面板(覆盖率治理一等公民,tech-design §2.4 #7)-->
            <div>
              <div class="text-xs font-medium chrome-text-heading mb-1">
                {{ t('lineage.parse_errors_title') }}
              </div>
              <div v-if="parseErrorCount === 0" class="text-xs chrome-text-muted">
                {{ t('lineage.parse_errors_none') }}
              </div>
              <template v-else>
                <div
                  v-for="(err, ei) in parseErrors"
                  :key="ei"
                  class="rounded-card border border-amber-300 dark:border-amber-500/40 bg-amber-50 dark:bg-amber-500/10 px-3 py-2 mb-1 text-xs text-amber-800 dark:text-amber-200"
                >
                  <span class="font-medium">{{ t('lineage.parse_error_stage') }}: {{ err.error_type ?? '-' }}</span>
                  ·
                  <span>
                    {{ t('lineage.parse_error_statement', { index: err.statement_index ?? '-' }) }}
                    <span v-if="err.statement_type" class="font-mono">({{ err.statement_type }})</span>
                  </span>
                  <div v-if="err.message" class="mt-0.5 font-mono">{{ err.message }}</div>
                </div>
                <div v-if="parseErrors.length === 0" class="text-xs chrome-text-muted">
                  {{ t('lineage.parse_error_detail_unavailable') }}
                </div>
              </template>
            </div>

            <div class="flex items-center gap-2 text-xs chrome-text-muted">
              {{ t('lineage.view_subgraph_hint') }}
              <button type="button" class="chrome-btn-secondary text-xs" @click="tab = 'subgraph'">
                <Waypoints class="w-3.5 h-3.5" /> {{ t('lineage.tab_subgraph') }}
              </button>
            </div>
          </div>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* SVG 子图 —— 用 chrome CSS 变量,跟随 variant 深浅色主题 */
.lineage-node {
  cursor: pointer;
}
.lineage-node-rect {
  fill: rgb(var(--bg-panel-elevated));
  stroke: rgb(var(--border));
  stroke-width: 1;
}
.lineage-node:hover .lineage-node-rect {
  stroke: rgb(var(--accent));
}
.lineage-node-rect-column {
  fill: rgb(var(--bg-panel));
}
.lineage-node-rect-focus {
  fill: rgb(var(--accent) / 0.12);
  stroke: rgb(var(--accent));
  stroke-width: 1.5;
}
.lineage-node-text {
  fill: rgb(var(--text-heading));
  font-size: 11px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  pointer-events: none;
}
.lineage-edge {
  stroke: rgb(var(--text-muted));
  stroke-width: 1.25;
  opacity: 0.75;
}
.lineage-edge-column {
  stroke-width: 1;
  opacity: 0.5;
}
/* AI 推断边:虚线 + amber,视觉区分确定性解析结果(tech-design §2.4 #7)*/
.lineage-edge-inferred {
  stroke: rgb(245 158 11); /* amber-500 */
  stroke-dasharray: 5 4;
}
.lineage-arrow {
  fill: rgb(var(--text-muted));
}
.lineage-legend-focus {
  display: inline-block;
  border-color: rgb(var(--accent));
  background-color: rgb(var(--accent) / 0.12);
}
</style>
