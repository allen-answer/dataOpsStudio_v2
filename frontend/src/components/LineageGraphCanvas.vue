<script setup lang="ts">
/**
 * L-9 血缘图(G6 canvas)—— 在 V1 血缘图基础上优化,首要目标治「卡顿」。
 *
 * V1 卡顿根因(考古结论):每次交互(搜索/hop/间距)都 destroy + new Graph +
 * 重跑 dagre;且旧的自绘 SVG 大图有 4N+3E 个活 DOM 节点。本组件的三条治卡顿手段:
 *   1) 单个 <canvas>(G6 v5 canvas 渲染)—— DOM 节点数恒为 ~1,消灭 SVG 爆炸;
 *   2) 图实例只建一次,数据变化用 graph.setData()(不 destroy 重建);
 *   3) 搜索/高亮走 graph.setElementState()(不重跑布局)+ 输入 debounce。
 *
 * 数据直接吃 2.0 子图接口的扁平 nodes/edges(LineageSubgraphResponse),
 * 无需 V1 的 groups 派生层。G6 懒加载(动态 import → g6-vendor chunk,不进主包)。
 */
import { ref, shallowRef, watch, onMounted, onBeforeUnmount, nextTick } from 'vue'
import type { Graph as G6Graph } from '@antv/g6'
import type { LineageSubgraphNode, LineageSubgraphEdge } from '../api/lineage'

const props = defineProps<{
  nodes: LineageSubgraphNode[]
  edges: LineageSubgraphEdge[]
  focus: string
}>()

const emit = defineEmits<{
  (e: 'node-click', id: string): void
  (e: 'edge-click', edgeId: string): void
}>()

const container = ref<HTMLDivElement | null>(null)
const graph = shallowRef<G6Graph | null>(null)
const loading = ref(true)
const loadError = ref<string | null>(null)
const search = ref('')
const matchCount = ref(0)

// ── 主题色:读 style.css 的 CSS 变量(空格分隔 RGB 三元组)──────────────
function cssVar(name: string, fallback: string): string {
  const el = container.value ?? document.documentElement
  const raw = getComputedStyle(el).getPropertyValue(name).trim()
  if (!raw) return fallback
  // 变量形如 "16 185 129" 或带 alpha "39 39 42 / 50%";图上只取 rgb 部分。
  const rgb = raw.split('/')[0].trim()
  return `rgb(${rgb})`
}

interface Palette {
  focusFill: string
  focusStroke: string
  tableFill: string
  tableStroke: string
  columnFill: string
  columnStroke: string
  text: string
  edge: string
  edgeInferred: string
  highlight: string
  dim: string
}

function palette(): Palette {
  const accent = cssVar('--accent', 'rgb(16 185 129)')
  const border = cssVar('--border', 'rgb(203 213 225)')
  const panel = cssVar('--bg-panel', 'rgb(255 255 255)')
  const elevated = cssVar('--bg-panel-elevated', 'rgb(226 232 240)')
  const text = cssVar('--text-heading', 'rgb(15 23 42)')
  return {
    focusFill: accent,
    focusStroke: accent,
    tableFill: elevated,
    tableStroke: border,
    columnFill: panel,
    columnStroke: border,
    text,
    edge: border,
    edgeInferred: 'rgb(217 119 6)', // amber-600:推断边(虚线)
    highlight: accent,
    dim: 'rgb(148 163 184)',
  }
}

// ── 2.0 扁平数据 → G6 data(过滤悬空边,防 G6 抛错)────────────────────
function toG6Data(): { nodes: unknown[]; edges: unknown[] } {
  const ids = new Set(props.nodes.map((n) => n.id))
  const nodes = props.nodes.map((n) => ({
    id: n.id,
    data: {
      kind: n.kind,
      label: n.label,
      table: n.table,
      column: n.column,
      depth: n.depth,
      isFocus: n.id === props.focus,
    },
  }))
  const edges = props.edges
    .filter((e) => ids.has(e.source) && ids.has(e.target))
    .map((e) => ({
      id: e.id,
      source: e.source,
      target: e.target,
      data: {
        inferred: e.inferred && e.inference_status !== 'confirmed',
        confidence: e.confidence,
        kind: e.edge_kind,
      },
    }))
  return { nodes, edges }
}

function graphConfig(pal: Palette) {
  return {
    container: container.value as HTMLDivElement,
    autoResize: true,
    autoFit: 'view' as const,
    padding: 24,
    data: toG6Data(),
    node: {
      type: 'rect',
      style: {
        size: (d: { data: Record<string, unknown> }) =>
          d.data.kind === 'column' ? [150, 28] : [170, 32],
        radius: 6,
        lineWidth: (d: { data: Record<string, unknown> }) => (d.data.isFocus ? 2.5 : 1.2),
        fill: (d: { data: Record<string, unknown> }) =>
          d.data.isFocus ? pal.focusFill : d.data.kind === 'column' ? pal.columnFill : pal.tableFill,
        stroke: (d: { data: Record<string, unknown> }) =>
          d.data.isFocus
            ? pal.focusStroke
            : d.data.kind === 'column'
              ? pal.columnStroke
              : pal.tableStroke,
        labelText: (d: { data: Record<string, unknown> }) => String(d.data.label ?? ''),
        labelPlacement: 'center',
        labelFill: (d: { data: Record<string, unknown> }) =>
          d.data.isFocus ? '#ffffff' : pal.text,
        labelFontSize: 11,
        labelMaxWidth: (d: { data: Record<string, unknown> }) =>
          d.data.kind === 'column' ? 140 : 160,
        labelWordWrap: false,
      },
      state: {
        highlight: { lineWidth: 3, stroke: pal.highlight, shadowColor: pal.highlight, shadowBlur: 8 },
        dim: { fillOpacity: 0.35, strokeOpacity: 0.35, labelOpacity: 0.35 },
        selected: { lineWidth: 3, stroke: pal.highlight },
      },
    },
    edge: {
      type: 'cubic-horizontal',
      style: {
        lineWidth: 1.2,
        stroke: (d: { data: Record<string, unknown> }) =>
          d.data.inferred ? pal.edgeInferred : pal.edge,
        lineDash: (d: { data: Record<string, unknown> }) => (d.data.inferred ? [4, 4] : undefined),
        endArrow: true,
        endArrowSize: 8,
      },
      state: {
        highlight: { lineWidth: 2.5, stroke: pal.highlight },
        dim: { strokeOpacity: 0.15 },
      },
    },
    layout: {
      type: 'antv-dagre',
      rankdir: 'LR',
      align: 'DL',
      nodesep: 18,
      ranksep: 90,
    },
    behaviors: ['drag-canvas', 'zoom-canvas', 'drag-element'],
    plugins: [{ type: 'minimap', size: [180, 120] as [number, number] }],
  }
}

let searchTimer: ReturnType<typeof setTimeout> | null = null

async function buildGraph(): Promise<void> {
  if (!container.value) return
  loading.value = true
  loadError.value = null
  try {
    const { Graph } = await import('@antv/g6')
    // 容器可能在 v-show 隐藏时挂载,等一帧确保拿到尺寸。
    await nextTick()
    const g = new Graph(graphConfig(palette()) as never)
    graph.value = g
    g.on('node:click', (evt) => {
      if ('target' in evt && evt.target && 'id' in evt.target && typeof evt.target.id === 'string') {
        emit('node-click', evt.target.id)
      }
    })
    g.on('edge:click', (evt) => {
      if ('target' in evt && evt.target && 'id' in evt.target && typeof evt.target.id === 'string') {
        emit('edge-click', evt.target.id)
      }
    })
    await g.render()
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

/** 数据变化:setData 复用同一实例(不 destroy 重建)—— 治卡顿关键。 */
async function refreshData(): Promise<void> {
  const g = graph.value
  if (!g) {
    await buildGraph()
    return
  }
  try {
    g.setData(toG6Data() as never)
    await g.render()
    applySearch()
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : String(e)
  }
}

/** 搜索高亮:只改元素 state,不重跑布局(V1 每次重建的痛点)。 */
function applySearch(): void {
  const g = graph.value
  if (!g) return
  const q = search.value.trim().toLowerCase()
  const states: Record<string, string[]> = {}
  if (!q) {
    for (const n of props.nodes) states[n.id] = []
    matchCount.value = 0
    void g.setElementState(states)
    return
  }
  const matched = new Set<string>()
  for (const n of props.nodes) {
    if (n.label.toLowerCase().includes(q) || n.table.toLowerCase().includes(q)) matched.add(n.id)
  }
  matchCount.value = matched.size
  for (const n of props.nodes) states[n.id] = matched.has(n.id) ? ['highlight'] : ['dim']
  void g.setElementState(states)
  // 命中时把视图对准第一个匹配节点。
  const first = props.nodes.find((n) => matched.has(n.id))
  if (first) void g.focusElement?.(first.id)
}

function onSearchInput(): void {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(applySearch, 220)
}

function fitView(): void {
  void graph.value?.fitView()
}
function zoomBy(ratio: number): void {
  const g = graph.value
  if (g) void g.zoomBy(ratio)
}

onMounted(buildGraph)

// 父组件每次查询都替换整个 subgraphData → nodes/edges 是新数组引用,
// 用引用比较即可(不 deep watch,避免大数组 diff 开销)。
watch(
  () => [props.nodes, props.edges],
  () => {
    void refreshData()
  },
)

onBeforeUnmount(() => {
  if (searchTimer) clearTimeout(searchTimer)
  graph.value?.destroy()
  graph.value = null
})

defineExpose({ fitView })
</script>

<template>
  <div class="lineage-graph-canvas">
    <div class="graph-toolbar">
      <input
        v-model="search"
        type="text"
        class="graph-search"
        :placeholder="'搜索表 / 列名…'"
        @input="onSearchInput"
      />
      <span v-if="search.trim()" class="graph-match">{{ matchCount }} 命中</span>
      <div class="graph-toolbar-spacer" />
      <button type="button" class="graph-btn" title="放大" @click="zoomBy(1.2)">＋</button>
      <button type="button" class="graph-btn" title="缩小" @click="zoomBy(0.8)">－</button>
      <button type="button" class="graph-btn" title="适应视图" @click="fitView">⤢</button>
    </div>

    <div ref="container" class="graph-stage">
      <div v-if="loading" class="graph-overlay">加载血缘图引擎…</div>
      <div v-else-if="loadError" class="graph-overlay graph-error">图渲染失败:{{ loadError }}</div>
    </div>

    <div class="graph-legend">
      <span class="lg"><i class="dot focus" />焦点</span>
      <span class="lg"><i class="dot table" />表</span>
      <span class="lg"><i class="dot column" />列</span>
      <span class="lg"><i class="line inferred" />推断边</span>
    </div>
  </div>
</template>

<style scoped>
.lineage-graph-canvas {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.graph-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
}
.graph-toolbar-spacer {
  flex: 1;
}
.graph-search {
  width: 220px;
  padding: 5px 10px;
  font-size: 13px;
  border-radius: 6px;
  border: 1px solid rgb(var(--border));
  background: rgb(var(--bg-panel));
  color: rgb(var(--text-heading));
}
.graph-search:focus {
  outline: none;
  border-color: rgb(var(--accent));
  box-shadow: 0 0 0 3px rgb(var(--accent) / 0.18);
}
.graph-match {
  font-size: 12px;
  color: rgb(var(--accent));
}
.graph-btn {
  width: 30px;
  height: 30px;
  border-radius: 6px;
  border: 1px solid rgb(var(--border));
  background: rgb(var(--bg-panel-elevated));
  color: rgb(var(--text-heading));
  font-size: 15px;
  line-height: 1;
  cursor: pointer;
}
.graph-btn:hover {
  border-color: rgb(var(--accent));
}
.graph-stage {
  position: relative;
  width: 100%;
  height: 560px;
  border: 1px solid rgb(var(--border));
  border-radius: 8px;
  background: rgb(var(--bg-main));
  overflow: hidden;
}
.graph-overlay {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  color: rgb(var(--text-heading));
  opacity: 0.75;
}
.graph-error {
  color: rgb(220 38 38);
}
.graph-legend {
  display: flex;
  gap: 16px;
  font-size: 12px;
  color: rgb(var(--text-heading));
  opacity: 0.8;
}
.graph-legend .lg {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}
.graph-legend .dot {
  width: 12px;
  height: 12px;
  border-radius: 3px;
  border: 1px solid rgb(var(--border));
}
.graph-legend .dot.focus {
  background: rgb(var(--accent));
  border-color: rgb(var(--accent));
}
.graph-legend .dot.table {
  background: rgb(var(--bg-panel-elevated));
}
.graph-legend .dot.column {
  background: rgb(var(--bg-panel));
}
.graph-legend .line {
  width: 16px;
  height: 0;
  border-top: 2px dashed rgb(217 119 6);
}
</style>
