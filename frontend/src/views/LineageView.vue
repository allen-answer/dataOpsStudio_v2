<script setup lang="ts">
/**
 * LineageView —— /projects/:id/lineage(2.3.0 血缘,tech-design §2.4 + ADR-0019)
 *
 * 四个 tab:
 *  - 子图查询(主视图):焦点表/列 N 跳邻域子图,自定义 SVG 分层布局
 *    (焦点居中,上游在左 / 下游在右,按 depth 分列)。★ 不做全景图(ADR-0019)。
 *  - 影响分析:焦点表 → 下游波及清单,按 depth 分组(纯图遍历,不依赖 AI)。
 *  - SQL 解析:选数据源 + 粘贴 SQL → analyze 端点落边;支持 refresh 绕过 sql_hash 缓存。
 *  - 批量分析(L-2):上传 SQL 脚本 ZIP → 后台 job 逐文件宽松解析 → 汇总报告
 *    (文件明细 + 跨脚本依赖);2s 轮询,换 tab / 卸载停轮询。
 *
 * 字段全部锚 api/lineage.ts(锚后端 schemas.py + core.py + worker.py),不臆造。
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useQuery } from '@tanstack/vue-query'
import {
  AlertTriangle,
  Check,
  ChevronDown,
  ChevronRight,
  Download,
  FileArchive,
  FileSearch,
  FileStack,
  Lightbulb,
  Network,
  Play,
  RefreshCw,
  RotateCcw,
  Sparkles,
  Target,
  Upload,
  Waypoints,
  X,
} from 'lucide-vue-next'
import { listDatasources } from '../api/datasources'
import { downloadExport } from '../api/metadata'
import {
  analyzeLineage,
  createLineageBatch,
  exportLineage,
  getLineageBatch,
  getLineageEdgeDetail,
  getLineageImpact,
  getLineageSubgraph,
  updateLineageEdge,
  type LineageBatchFileEntry,
  type LineageBatchJobStatus,
  type LineageBatchReport,
  type LineageEdgeDetailResponse,
  type LineageAnalyzeResponse,
  type LineageDirection,
  type LineageImpactItem,
  type LineageImpactResponse,
  type LineageInferenceDecision,
  type LineageSubgraphEdge,
  type LineageSubgraphNode,
  type LineageSubgraphResponse,
  type LineageTargetCounts,
} from '../api/lineage'
import { uploadFile } from '../api/uploads'
import { ApiError, type DatasourceListItem } from '../api/types'
import LoadingDots from '../components/LoadingDots.vue'

type Tab = 'subgraph' | 'impact' | 'analyze' | 'batch'

// 血缘解析器支持的方言(app/domain/lineage/parser.py _normalize_dialect)
const LINEAGE_DIALECTS: ReadonlySet<string> = new Set(['mysql', 'oracle', 'dm', 'postgresql'])

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

// ── 子图导出(L-5:POST /lineage/export,同步 201 + 一次性 token)─────
const exportBusy = ref(false)
const exportError = ref<string | null>(null)

/** 用「已查出的子图」的参数导出(与画面一致),拿到 token 后立即触发下载。 */
async function onExportSubgraph(): Promise<void> {
  const data = subgraphData.value
  if (!projectId.value || !data || exportBusy.value) return
  exportBusy.value = true
  exportError.value = null
  try {
    const res = await exportLineage(projectId.value, {
      focus: data.focus,
      direction: data.direction,
      max_depth: data.max_depth,
      include_columns: data.include_columns,
    })
    await downloadExport(res.download_token, res.filename)
  } catch (e) {
    exportError.value = exportErrorMessage(e)
  } finally {
    exportBusy.value = false
  }
}

function exportErrorMessage(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 429) return t('lineage.export_rate_limited')
    if (e.status === 410) return t('lineage.export_token_spent')
  }
  return errorMessage(e)
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

// ── 边详情抽屉(UX P0-L1:点边看"这条边为什么存在")──────────────
const edgeDetailOpen = ref(false)
const edgeDetailLoading = ref(false)
const edgeDetailError = ref<string | null>(null)
const edgeDetail = ref<LineageEdgeDetailResponse | null>(null)
const edgeSqlCopied = ref(false)

async function onEdgeClick(edge: LineageSubgraphEdge): Promise<void> {
  if (!projectId.value) return
  edgeDetailOpen.value = true
  edgeDetailLoading.value = true
  edgeDetailError.value = null
  edgeDetail.value = null
  edgeSqlCopied.value = false
  try {
    edgeDetail.value = await getLineageEdgeDetail(projectId.value, edge.id)
  } catch (e) {
    edgeDetailError.value = errorMessage(e)
  } finally {
    edgeDetailLoading.value = false
  }
}

async function copyEdgeSql(): Promise<void> {
  const sql = edgeDetail.value?.run?.sql_text
  if (!sql) return
  await navigator.clipboard.writeText(sql)
  edgeSqlCopied.value = true
  window.setTimeout(() => {
    edgeSqlCopied.value = false
  }, 1500)
}

// ── 图内定位(UX P0-L3:Find in canvas)────────────────────────────
const canvasQuery = ref('')
const canvasMatchId = ref<string | null>(null)
const canvasMiss = ref(false)
const svgScroller = ref<HTMLDivElement | null>(null)

function locateInCanvas(): void {
  canvasMiss.value = false
  canvasMatchId.value = null
  const query = canvasQuery.value.trim().toLowerCase()
  if (!query || !graphLayout.value) return
  const hit = graphLayout.value.nodes.find((ln) => ln.node.id.toLowerCase().includes(query))
  if (!hit) {
    canvasMiss.value = true
    return
  }
  canvasMatchId.value = hit.node.id
  const scroller = svgScroller.value
  if (scroller) {
    scroller.scrollTo({
      left: Math.max(0, hit.x - scroller.clientWidth / 2 + NODE_W / 2),
      top: Math.max(0, hit.y - scroller.clientHeight / 2 + NODE_H / 2),
      behavior: 'smooth',
    })
  }
}

// ── AI 推断边审核(L-6:PATCH /lineage/edges/{edge_id})────────────
// 待审核 = inference_status 仍是 'inferred' 的推断边;direction=both 时同一条边
// 可能双向各出现一次(模板 key 用 id+direction),这里按 id 去重。
const inferredEdges = computed<LineageSubgraphEdge[]>(() => {
  const seen = new Set<string>()
  const list: LineageSubgraphEdge[] = []
  for (const edge of subgraphData.value?.edges ?? []) {
    if (!edge.inferred || edge.inference_status !== 'inferred' || seen.has(edge.id)) continue
    seen.add(edge.id)
    list.push(edge)
  }
  return list
})

const inferencePendingId = ref<string | null>(null)
const inferenceError = ref<string | null>(null)

/**
 * 确认 / 拒绝一条 AI 推断边,成功后重查子图(rejected 边被后端 CTE 排除,
 * 会直接从图里消失)。409 invalid_inference_transition = 状态已被别处改过,
 * 给友好提示并刷新子图对齐真实状态。
 */
async function decideEdge(
  edge: LineageSubgraphEdge,
  status: LineageInferenceDecision,
): Promise<void> {
  if (!projectId.value || inferencePendingId.value) return
  inferencePendingId.value = edge.id
  inferenceError.value = null
  try {
    await updateLineageEdge(projectId.value, edge.id, status)
    await runSubgraph()
  } catch (e) {
    if (e instanceof ApiError && e.code === 'invalid_inference_transition') {
      inferenceError.value = t('lineage.inference_conflict')
      await runSubgraph()
    } else {
      inferenceError.value = errorMessage(e)
    }
  } finally {
    inferencePendingId.value = null
  }
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

// ── 批量分析(L-2:ZIP 上传 → job → 报告)──────────────────────────
const MAX_UPLOAD_BYTES = 100 * 1024 * 1024 // 与后端 upload_max_mb 一致(413 前先拦)
const BATCH_POLL_MS = 2000

type BatchPhase = 'idle' | 'uploading' | 'running' | 'done' | 'failed'

// batchDsId === '' 表示「无数据库(纯文本导入)」;此时用 batchManualDialect
const batchDsId = ref('')
const batchManualDialect = ref('oracle')
const batchDefaultSchema = ref('')
const batchFile = ref<File | null>(null)
const batchPhase = ref<BatchPhase>('idle')
const batchError = ref<string | null>(null)
const batchJobId = ref<string | null>(null)
const batchJobStatus = ref<LineageBatchJobStatus | null>(null)
const batchReport = ref<LineageBatchReport | null>(null)
const expandedFiles = ref<Set<string>>(new Set())
let batchPollTimer: ReturnType<typeof setTimeout> | null = null

const BATCH_DIALECTS = ['mysql', 'oracle', 'dm', 'postgresql'] as const

watch(datasources, (list) => {
  if (!batchDsId.value && list.length > 0) batchDsId.value = list[0].id
})

const batchNoDb = computed(() => batchDsId.value === '')
// 有库:方言跟随数据源;无库:用户手选
const batchDialect = computed(() =>
  batchNoDb.value
    ? batchManualDialect.value
    : (datasources.value.find((ds) => ds.id === batchDsId.value)?.db_type ?? ''),
)
const batchDialectUnsupported = computed(
  () => Boolean(batchDialect.value) && !LINEAGE_DIALECTS.has(batchDialect.value),
)
const batchBusy = computed(
  () => batchPhase.value === 'uploading' || batchPhase.value === 'running',
)

function onBatchFileChange(event: Event): void {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0] ?? null
  batchError.value = null
  if (file && file.size > MAX_UPLOAD_BYTES) {
    batchError.value = t('lineage.batch_file_too_large')
    batchFile.value = null
    input.value = ''
    return
  }
  batchFile.value = file
}

function stopBatchPolling(): void {
  if (batchPollTimer !== null) {
    clearTimeout(batchPollTimer)
    batchPollTimer = null
  }
}

function toggleFileRow(sourceRef: string): void {
  const next = new Set(expandedFiles.value)
  if (next.has(sourceRef)) next.delete(sourceRef)
  else next.add(sourceRef)
  expandedFiles.value = next
}

async function pollBatch(): Promise<void> {
  const jobId = batchJobId.value
  if (!projectId.value || !jobId) return
  try {
    const res = await getLineageBatch(projectId.value, jobId)
    batchJobStatus.value = res.status
    if (res.status === 'success') {
      batchReport.value = res.report
      batchPhase.value = 'done'
      stopBatchPolling()
      return
    }
    if (res.status === 'failed' || res.status === 'cancelled' || res.status === 'timeout') {
      batchError.value = res.error || t('lineage.batch_job_failed')
      batchPhase.value = 'failed'
      stopBatchPolling()
      return
    }
    // pending / running:继续轮询
    batchPollTimer = setTimeout(() => void pollBatch(), BATCH_POLL_MS)
  } catch (e) {
    batchError.value = errorMessage(e)
    batchPhase.value = 'failed'
    stopBatchPolling()
  }
}

async function onBatchSubmit(): Promise<void> {
  // 数据源可选:无库模式只需文件 + dialect(下拉恒有值);有库模式需选中数据源
  if (!projectId.value || !batchFile.value) {
    batchError.value = t('lineage.batch_required')
    return
  }
  stopBatchPolling()
  batchError.value = null
  batchReport.value = null
  batchJobStatus.value = null
  expandedFiles.value = new Set()
  batchPhase.value = 'uploading'
  try {
    const upload = await uploadFile(projectId.value, batchFile.value, 'lineage_batch')
    batchPhase.value = 'running'
    const { job_id } = await createLineageBatch(projectId.value, {
      upload_id: upload.upload_id,
      datasource_id: batchNoDb.value ? null : batchDsId.value,
      // 无库必须显式 dialect;有库留空由后端按 db_type 推导
      dialect: batchNoDb.value ? batchManualDialect.value : null,
      default_schema: batchDefaultSchema.value.trim() || null,
    })
    batchJobId.value = job_id
    await pollBatch()
  } catch (e) {
    if (e instanceof ApiError && e.code === 'upload_too_large') {
      batchError.value = t('lineage.batch_file_too_large')
    } else {
      batchError.value = errorMessage(e)
    }
    batchPhase.value = 'failed'
  }
}

function resetBatch(): void {
  stopBatchPolling()
  batchFile.value = null
  batchPhase.value = 'idle'
  batchError.value = null
  batchJobId.value = null
  batchJobStatus.value = null
  batchReport.value = null
  expandedFiles.value = new Set()
}

function batchFileHasDetail(file: LineageBatchFileEntry): boolean {
  return (
    file.status === 'failed' ||
    Number(file.parse_error_count ?? 0) > 0 ||
    (file.tables_written?.length ?? 0) > 0 ||
    (file.tables_read?.length ?? 0) > 0
  )
}

// L-4 语义视图:写入计数压成 I/U/M/D/T 简写(全 0 显示破折号)
function semanticCounts(c: LineageTargetCounts): string {
  const parts: string[] = []
  if (c.insert) parts.push(`I${c.insert}`)
  if (c.update) parts.push(`U${c.update}`)
  if (c.merge) parts.push(`M${c.merge}`)
  if (c.delete) parts.push(`D${c.delete}`)
  if (c.truncate) parts.push(`T${c.truncate}`)
  return parts.length ? parts.join(' ') : '—'
}

// refresh_mode → i18n 标签(未知值原样回退,不抛 missing-key)
function semanticRefreshLabel(mode: string | null): string {
  if (!mode) return '—'
  const key = `lineage.semantic_refresh_${mode}`
  const label = t(key)
  return label === key ? mode : label
}

// 换 tab 离开批量视图不停轮询(job 在后端继续跑,回来仍可看);仅卸载时停
onBeforeUnmount(stopBatchPolling)

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
      <button
        type="button"
        class="chrome-tab"
        :class="tab === 'batch' && 'chrome-accent-light-bg chrome-accent'"
        @click="tab = 'batch'"
      >
        <FileArchive class="w-4 h-4" /> {{ t('lineage.tab_batch') }}
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
            <button
              v-if="subgraphData.edge_count > 0"
              type="button"
              class="chrome-btn-secondary text-xs ml-auto"
              :disabled="exportBusy"
              @click="onExportSubgraph"
            >
              <Download class="w-3.5 h-3.5" :class="exportBusy && 'animate-pulse'" />
              {{ exportBusy ? t('lineage.exporting') : t('lineage.export_excel') }}
            </button>
          </div>
          <div v-if="exportError" class="text-xs text-red-600 dark:text-red-400">
            {{ exportError }}
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

          <!-- 子图 SVG(按 depth 分列,焦点列高亮;点节点切焦点,点边开抽屉)-->
          <template v-else-if="graphLayout">
            <!-- 图内定位(UX P0-L3)-->
            <div class="flex items-center gap-2">
              <input
                v-model="canvasQuery"
                type="text"
                class="chrome-input text-xs w-56"
                :placeholder="t('lineage.canvas_search_ph')"
                @keydown.enter.prevent="locateInCanvas"
              />
              <button type="button" class="chrome-btn-secondary text-xs" @click="locateInCanvas">
                <Target class="w-3.5 h-3.5" /> {{ t('lineage.canvas_locate') }}
              </button>
              <span v-if="canvasMiss" class="text-[11px] text-amber-600 dark:text-amber-400">
                {{ t('lineage.canvas_search_miss') }}
              </span>
            </div>
            <div
              ref="svgScroller"
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
                <!-- 每条边 = 透明加宽 hit-area(承接点击/悬浮)+ 可见细线(不接事件) -->
                <g
                  v-for="le in graphLayout.edges"
                  :key="`${le.edge.id}-${le.edge.direction}`"
                  class="lineage-edge-group"
                  @click="onEdgeClick(le.edge)"
                >
                  <path :d="le.d" fill="none" stroke="transparent" stroke-width="12">
                    <title>{{ edgeTitle(le.edge) }}</title>
                  </path>
                  <path
                    :d="le.d"
                    fill="none"
                    marker-end="url(#lineage-arrow)"
                    class="lineage-edge pointer-events-none"
                    :class="{
                      'lineage-edge-column': le.edge.edge_kind === 'column',
                      'lineage-edge-inferred': le.edge.inferred,
                    }"
                  />
                </g>
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
                  <rect
                    v-if="ln.node.id === canvasMatchId"
                    :width="NODE_W"
                    :height="NODE_H"
                    rx="6"
                    class="lineage-node-locate-ring"
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
              <span>{{ t('lineage.edge_click_hint') }}</span>
            </div>

            <!-- 边详情抽屉(UX P0-L1)-->
            <Teleport to="body">
              <div
                v-if="edgeDetailOpen"
                class="fixed inset-0 z-40 bg-black/30"
                @click="edgeDetailOpen = false"
              />
              <aside
                v-if="edgeDetailOpen"
                class="fixed right-0 top-0 z-50 h-full w-[440px] max-w-[92vw] chrome-bg-panel border-l chrome-border shadow-xl flex flex-col"
              >
                <div class="flex items-center justify-between px-4 py-3 border-b chrome-border-subtle">
                  <span class="text-sm font-medium chrome-text-heading">
                    {{ t('lineage.edge_detail_title') }}
                  </span>
                  <button
                    type="button"
                    class="chrome-btn-ghost"
                    :aria-label="t('common.close')"
                    @click="edgeDetailOpen = false"
                  >
                    <X class="w-4 h-4" />
                  </button>
                </div>
                <div class="flex-1 overflow-auto p-4 space-y-3 text-xs">
                  <LoadingDots v-if="edgeDetailLoading" />
                  <div v-else-if="edgeDetailError" class="text-red-600 dark:text-red-400">
                    {{ edgeDetailError }}
                  </div>
                  <template v-else-if="edgeDetail">
                    <div class="font-mono chrome-text-heading break-all">
                      {{ edgeDetail.source_column ? `${edgeDetail.source_table}.${edgeDetail.source_column}` : edgeDetail.source_table }}
                      →
                      {{ edgeDetail.target_column ? `${edgeDetail.target_table}.${edgeDetail.target_column}` : edgeDetail.target_table }}
                    </div>
                    <dl class="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 chrome-text-muted">
                      <dt>{{ t('lineage.edge_kind') }}</dt>
                      <dd class="chrome-text-heading">{{ edgeDetail.edge_kind }}</dd>
                      <template v-if="edgeDetail.transformation">
                        <dt>{{ t('lineage.edge_transformation') }}</dt>
                        <dd class="chrome-text-heading">
                          {{ edgeDetail.transformation
                          }}<template v-if="edgeDetail.transformation_subtype && edgeDetail.transformation_subtype !== edgeDetail.transformation">
                            / {{ edgeDetail.transformation_subtype }}</template>
                        </dd>
                      </template>
                      <dt>{{ t('lineage.edge_status') }}</dt>
                      <dd class="chrome-text-heading">
                        {{ edgeDetail.inference_status }}
                        <template v-if="edgeDetail.inferred">
                          · {{ t('lineage.inferred_confidence', { confidence: edgeDetail.confidence.toFixed(2) }) }}
                        </template>
                      </dd>
                      <template v-if="edgeDetail.run">
                        <dt>{{ t('lineage.edge_run_source_ref') }}</dt>
                        <dd class="chrome-text-heading break-all">{{ edgeDetail.run.source_ref }}</dd>
                        <dt>{{ t('lineage.edge_run_dialect') }}</dt>
                        <dd class="chrome-text-heading">{{ edgeDetail.run.dialect }}</dd>
                        <dt>{{ t('lineage.edge_run_created') }}</dt>
                        <dd class="chrome-text-heading">{{ edgeDetail.run.created_at }}</dd>
                      </template>
                    </dl>
                    <div v-if="edgeDetail.run?.sql_text" class="space-y-1.5">
                      <div class="flex items-center justify-between">
                        <span class="font-medium chrome-text-heading">{{ t('lineage.edge_sql') }}</span>
                        <button type="button" class="chrome-btn-secondary text-xs" @click="copyEdgeSql">
                          {{ edgeSqlCopied ? t('common.copied') : t('common.copy') }}
                        </button>
                      </div>
                      <pre class="font-mono text-[11px] leading-relaxed whitespace-pre-wrap break-all rounded-card border chrome-border chrome-bg-elevated p-3 max-h-[48vh] overflow-auto">{{ edgeDetail.run.sql_text }}</pre>
                    </div>
                    <p v-else class="chrome-text-muted">{{ t('lineage.edge_sql_missing') }}</p>
                  </template>
                </div>
              </aside>
            </Teleport>

            <!-- AI 推断边审核面板(L-6):inferred 边逐条 确认/拒绝 -->
            <div v-if="inferenceError" class="text-xs text-red-600 dark:text-red-400">
              {{ inferenceError }}
            </div>
            <div
              v-if="inferredEdges.length > 0"
              class="rounded-card border chrome-border chrome-bg-panel p-3 space-y-2"
            >
              <div class="flex items-center gap-2 text-xs font-medium chrome-text-heading">
                <Sparkles class="w-4 h-4 text-amber-500" />
                {{ t('lineage.inferred_panel_title', { count: inferredEdges.length }) }}
              </div>
              <p class="text-[11px] chrome-text-muted">{{ t('lineage.inferred_panel_hint') }}</p>
              <div
                v-for="edge in inferredEdges"
                :key="edge.id"
                class="flex flex-wrap items-center gap-2 rounded-card border chrome-border px-3 py-2"
              >
                <span class="flex-1 min-w-[12rem] text-xs font-mono chrome-text-heading">
                  {{ edge.source }} → {{ edge.target }}
                </span>
                <span class="text-[11px] chrome-text-muted tabular-nums">
                  {{ t('lineage.inferred_confidence', { confidence: edge.confidence.toFixed(2) }) }}
                </span>
                <button
                  type="button"
                  class="chrome-btn-secondary text-xs"
                  :disabled="inferencePendingId !== null"
                  @click="decideEdge(edge, 'confirmed')"
                >
                  <Check class="w-3.5 h-3.5" /> {{ t('lineage.inference_confirm') }}
                </button>
                <button
                  type="button"
                  class="chrome-btn-secondary text-xs"
                  :disabled="inferencePendingId !== null"
                  @click="decideEdge(edge, 'rejected')"
                >
                  <X class="w-3.5 h-3.5" /> {{ t('lineage.inference_reject') }}
                </button>
              </div>
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

      <!-- ============ 批量分析 tab(L-2:ZIP → job → 报告)============ -->
      <div v-show="tab === 'batch'" class="p-4 space-y-4 max-w-4xl">
        <p class="text-xs chrome-text-muted">{{ t('lineage.batch_hint') }}</p>
          <div class="grid grid-cols-2 gap-4">
            <label class="block">
              <span class="block text-xs chrome-text-muted mb-1">{{ t('lineage.datasource') }}</span>
              <select
                v-model="batchDsId"
                class="chrome-input w-full text-sm"
                :disabled="batchBusy"
              >
                <option value="">{{ t('lineage.batch_no_db') }}</option>
                <option v-for="ds in datasources" :key="ds.id" :value="ds.id">{{ ds.name }}</option>
              </select>
              <!-- 无库:手选方言;有库:方言跟随 db_type -->
              <select
                v-if="batchNoDb"
                v-model="batchManualDialect"
                class="chrome-input w-full text-sm mt-1"
                :disabled="batchBusy"
              >
                <option v-for="d in BATCH_DIALECTS" :key="d" :value="d">{{ d }}</option>
              </select>
              <span v-else class="block mt-1 text-[11px] chrome-text-muted">
                {{ t('lineage.dialect_follow', { dialect: batchDialect }) }}
              </span>
              <span v-if="batchNoDb" class="block mt-1 text-[11px] chrome-text-muted">
                {{ t('lineage.batch_no_db_hint') }}
              </span>
            </label>
            <label class="block">
              <span class="block text-xs chrome-text-muted mb-1">
                {{ t('lineage.default_schema') }}
              </span>
              <input
                v-model="batchDefaultSchema"
                type="text"
                class="chrome-input w-full text-sm"
                :placeholder="t('lineage.default_schema_ph')"
                :disabled="batchBusy"
              />
            </label>
          </div>

          <div
            v-if="batchDialectUnsupported"
            class="flex items-center gap-2 rounded-card border border-amber-300 dark:border-amber-500/40 bg-amber-50 dark:bg-amber-500/10 px-3 py-2 text-xs text-amber-800 dark:text-amber-200"
          >
            <AlertTriangle class="w-4 h-4 shrink-0" />
            {{ t('lineage.dialect_unsupported', { db: batchDialect }) }}
          </div>

          <label class="block">
            <span class="block text-xs chrome-text-muted mb-1">{{ t('lineage.batch_file') }}</span>
            <input
              type="file"
              accept=".zip"
              class="block w-full text-sm chrome-text-heading file:mr-3 file:rounded-card file:border file:chrome-border file:chrome-bg-elevated file:px-3 file:py-1 file:text-xs file:chrome-text-heading"
              :disabled="batchBusy"
              @change="onBatchFileChange"
            />
            <span v-if="batchFile" class="block mt-1 text-[11px] chrome-text-muted">
              {{ batchFile.name }} · {{ (batchFile.size / 1024).toFixed(0) }} KB
            </span>
          </label>

          <div v-if="batchError" class="text-xs text-red-600 dark:text-red-400">
            {{ batchError }}
          </div>

          <div class="flex items-center gap-2">
            <button
              type="button"
              class="chrome-btn-primary text-sm"
              :disabled="batchBusy || !batchFile"
              @click="onBatchSubmit"
            >
              <Upload class="w-4 h-4" />
              <template v-if="batchPhase === 'uploading'">{{ t('lineage.batch_uploading') }}</template>
              <template v-else-if="batchPhase === 'running'">{{ t('lineage.batch_running') }}</template>
              <template v-else>{{ t('lineage.batch_submit') }}</template>
            </button>
            <button
              v-if="batchPhase === 'done' || batchPhase === 'failed'"
              type="button"
              class="chrome-btn-secondary text-sm"
              @click="resetBatch"
            >
              <RotateCcw class="w-4 h-4" /> {{ t('lineage.batch_reset') }}
            </button>
            <LoadingDots v-if="batchBusy" />
          </div>

          <!-- 报告 -->
          <div v-if="batchReport" class="space-y-4">
            <!-- 统计卡 -->
            <div class="grid grid-cols-2 sm:grid-cols-5 gap-2">
              <div class="rounded-card border chrome-border px-3 py-2">
                <div class="text-[11px] chrome-text-muted">{{ t('lineage.batch_stat_files') }}</div>
                <div class="text-lg font-semibold chrome-text-heading">
                  {{ batchReport.file_count }}
                </div>
              </div>
              <div class="rounded-card border chrome-border px-3 py-2">
                <div class="text-[11px] chrome-text-muted">{{ t('lineage.batch_stat_parsed') }}</div>
                <div class="text-lg font-semibold text-emerald-600 dark:text-emerald-400">
                  {{ batchReport.parsed }}
                </div>
              </div>
              <div class="rounded-card border chrome-border px-3 py-2">
                <div class="text-[11px] chrome-text-muted">{{ t('lineage.batch_stat_failed') }}</div>
                <div
                  class="text-lg font-semibold"
                  :class="batchReport.failed > 0 ? 'text-red-600 dark:text-red-400' : 'chrome-text-heading'"
                >
                  {{ batchReport.failed }}
                </div>
              </div>
              <div class="rounded-card border chrome-border px-3 py-2">
                <div class="text-[11px] chrome-text-muted">{{ t('lineage.batch_stat_edges') }}</div>
                <div class="text-lg font-semibold chrome-text-heading">
                  {{ batchReport.table_edge_total }}
                </div>
              </div>
              <div class="rounded-card border chrome-border px-3 py-2">
                <div class="text-[11px] chrome-text-muted">
                  {{ t('lineage.batch_stat_script_edges') }}
                </div>
                <div class="text-lg font-semibold chrome-text-heading">
                  {{ batchReport.script_edges.length }}
                </div>
              </div>
            </div>
            <div
              v-if="batchReport.skipped.non_sql + batchReport.skipped.too_large + batchReport.skipped.over_file_limit > 0"
              class="text-[11px] chrome-text-muted"
            >
              {{
                t('lineage.batch_skipped', {
                  non_sql: batchReport.skipped.non_sql,
                  too_large: batchReport.skipped.too_large,
                  over_limit: batchReport.skipped.over_file_limit,
                })
              }}
            </div>

            <!-- 文件明细表 -->
            <div class="rounded-card border chrome-border overflow-hidden">
              <div class="px-3 py-2 text-xs font-medium chrome-text-heading border-b chrome-border-subtle">
                {{ t('lineage.batch_files_title') }}
              </div>
              <div class="max-h-80 overflow-auto divide-y chrome-border-subtle">
                <div v-for="file in batchReport.files" :key="file.source_ref" class="text-xs">
                  <div
                    class="flex items-center gap-2 px-3 py-1.5"
                    :class="batchFileHasDetail(file) ? 'cursor-pointer hover:chrome-bg-elevated' : ''"
                    @click="batchFileHasDetail(file) && toggleFileRow(file.source_ref)"
                  >
                    <component
                      :is="expandedFiles.has(file.source_ref) ? ChevronDown : ChevronRight"
                      v-if="batchFileHasDetail(file)"
                      class="w-3.5 h-3.5 shrink-0 chrome-text-muted"
                    />
                    <span v-else class="w-3.5 shrink-0" />
                    <span
                      class="inline-block w-1.5 h-1.5 rounded-full shrink-0"
                      :class="file.status === 'failed' ? 'bg-red-500' : file.status === 'cached' ? 'bg-sky-400' : 'bg-emerald-500'"
                    />
                    <span class="flex-1 min-w-0 truncate font-mono chrome-text-heading" :title="file.source_ref">
                      {{ file.source_ref }}
                    </span>
                    <span class="shrink-0 chrome-text-muted tabular-nums">
                      {{ t('lineage.batch_file_edges', { count: file.table_edge_count ?? 0 }) }}
                    </span>
                    <span
                      v-if="(file.parse_error_count ?? 0) > 0"
                      class="shrink-0 text-amber-600 dark:text-amber-400 tabular-nums"
                    >
                      {{ t('lineage.batch_file_errors', { count: file.parse_error_count }) }}
                    </span>
                    <span
                      v-if="(file.lenient_statement_count ?? 0) > 0"
                      class="shrink-0 text-sky-600 dark:text-sky-400 tabular-nums"
                      :title="t('lineage.batch_lenient_hint')"
                    >
                      {{ t('lineage.batch_file_lenient', { count: file.lenient_statement_count }) }}
                    </span>
                  </div>
                  <div
                    v-if="expandedFiles.has(file.source_ref)"
                    class="px-3 pb-2 pl-8 space-y-1 chrome-bg-elevated"
                  >
                    <div v-if="file.error" class="text-red-600 dark:text-red-400">
                      {{ file.error }}
                    </div>
                    <div v-if="file.tables_written?.length" class="flex flex-wrap items-center gap-1">
                      <span class="chrome-text-muted">{{ t('lineage.batch_tables_written') }}:</span>
                      <span
                        v-for="tbl in file.tables_written"
                        :key="tbl"
                        class="rounded px-1.5 py-0.5 chrome-bg-panel font-mono"
                      >
                        {{ tbl }}
                      </span>
                    </div>
                    <div v-if="file.tables_read?.length" class="flex flex-wrap items-center gap-1">
                      <span class="chrome-text-muted">{{ t('lineage.batch_tables_read') }}:</span>
                      <span
                        v-for="tbl in file.tables_read"
                        :key="tbl"
                        class="rounded px-1.5 py-0.5 chrome-bg-panel font-mono"
                      >
                        {{ tbl }}
                      </span>
                    </div>
                  </div>
                </div>
              </div>
            </div>

            <!-- 跨脚本依赖 -->
            <div v-if="batchReport.script_edges.length" class="rounded-card border chrome-border overflow-hidden">
              <div class="px-3 py-2 text-xs font-medium chrome-text-heading border-b chrome-border-subtle">
                {{ t('lineage.batch_script_edges_title') }}
              </div>
              <div class="max-h-64 overflow-auto divide-y chrome-border-subtle">
                <div
                  v-for="(edge, ei) in batchReport.script_edges"
                  :key="ei"
                  class="flex flex-wrap items-center gap-2 px-3 py-1.5 text-xs"
                >
                  <span class="font-mono chrome-text-heading truncate max-w-[14rem]" :title="edge.source_file">
                    {{ edge.source_file }}
                  </span>
                  <span class="chrome-text-muted">→</span>
                  <span class="font-mono chrome-text-heading truncate max-w-[14rem]" :title="edge.target_file">
                    {{ edge.target_file }}
                  </span>
                  <span class="chrome-text-muted">·</span>
                  <span
                    v-for="tbl in edge.tables"
                    :key="tbl"
                    class="rounded px-1.5 py-0.5 chrome-bg-elevated font-mono"
                  >
                    {{ tbl }}
                  </span>
                </div>
              </div>
            </div>

            <!-- L-4 语义视图 / 目标表整合 -->
            <div v-if="batchReport.semantic_view" class="space-y-4">
              <!-- observations:人话观察 -->
              <div
                v-if="batchReport.semantic_view.observations.length"
                class="rounded-card border chrome-border overflow-hidden"
              >
                <div
                  class="flex items-center gap-2 px-3 py-2 text-xs font-medium chrome-text-heading border-b chrome-border-subtle"
                >
                  <Sparkles class="w-3.5 h-3.5 shrink-0" />
                  {{ t('lineage.semantic_observations_title') }}
                </div>
                <div class="divide-y chrome-border-subtle">
                  <div
                    v-for="(obs, oi) in batchReport.semantic_view.observations"
                    :key="oi"
                    class="flex items-start gap-2 px-3 py-1.5 text-xs chrome-text-heading"
                  >
                    <Lightbulb class="w-3.5 h-3.5 shrink-0 mt-0.5 chrome-text-muted" />
                    <span class="flex-1 min-w-0">{{ obs }}</span>
                  </div>
                </div>
              </div>

              <!-- risks:按 level 上色 -->
              <div
                v-if="batchReport.semantic_view.risks.length"
                class="rounded-card border chrome-border overflow-hidden"
              >
                <div
                  class="flex items-center gap-2 px-3 py-2 text-xs font-medium chrome-text-heading border-b chrome-border-subtle"
                >
                  <AlertTriangle class="w-3.5 h-3.5 shrink-0" />
                  {{ t('lineage.semantic_risks_title') }}
                </div>
                <div class="divide-y chrome-border-subtle">
                  <div
                    v-for="(risk, ri) in batchReport.semantic_view.risks"
                    :key="ri"
                    class="flex items-start gap-2 px-3 py-1.5 text-xs"
                    :class="
                      risk.level === 'high'
                        ? 'text-red-700 dark:text-red-300 bg-red-50 dark:bg-red-500/10'
                        : risk.level === 'medium'
                          ? 'text-amber-800 dark:text-amber-200 bg-amber-50 dark:bg-amber-500/10'
                          : 'chrome-text-muted'
                    "
                  >
                    <AlertTriangle class="w-3.5 h-3.5 shrink-0 mt-0.5" />
                    <span class="flex-1 min-w-0">{{ risk.message }}</span>
                  </div>
                </div>
              </div>

              <!-- targets:目标表整合表格 -->
              <div
                v-if="batchReport.semantic_view.targets.length"
                class="rounded-card border chrome-border overflow-hidden"
              >
                <div
                  class="flex items-center gap-2 px-3 py-2 text-xs font-medium chrome-text-heading border-b chrome-border-subtle"
                >
                  <FileStack class="w-3.5 h-3.5 shrink-0" />
                  {{ t('lineage.semantic_targets_title') }}
                </div>
                <div class="max-h-80 overflow-auto">
                  <table class="w-full text-xs">
                    <thead>
                      <tr class="text-left chrome-text-muted border-b chrome-border-subtle">
                        <th class="px-3 py-1.5 font-medium">{{ t('lineage.semantic_col_table') }}</th>
                        <th class="px-3 py-1.5 font-medium">{{ t('lineage.semantic_col_role') }}</th>
                        <th class="px-3 py-1.5 font-medium">{{ t('lineage.semantic_col_refresh') }}</th>
                        <th class="px-3 py-1.5 font-medium">
                          <span :title="t('lineage.semantic_counts_hint')">
                            {{ t('lineage.semantic_col_counts') }}
                          </span>
                        </th>
                        <th class="px-3 py-1.5 font-medium">{{ t('lineage.semantic_col_sources') }}</th>
                      </tr>
                    </thead>
                    <tbody class="divide-y chrome-border-subtle">
                      <tr
                        v-for="(tgt, ti) in batchReport.semantic_view.targets"
                        :key="ti"
                        class="chrome-text-heading"
                      >
                        <td class="px-3 py-1.5 font-mono" :title="tgt.table">{{ tgt.table }}</td>
                        <td class="px-3 py-1.5">
                          <span class="rounded px-1.5 py-0.5 chrome-bg-elevated">
                            {{ tgt.primary_role }}
                          </span>
                        </td>
                        <td class="px-3 py-1.5 chrome-text-muted">
                          {{ semanticRefreshLabel(tgt.refresh_mode) }}
                        </td>
                        <td class="px-3 py-1.5 tabular-nums chrome-text-muted">
                          {{ semanticCounts(tgt.counts) }}
                        </td>
                        <td class="px-3 py-1.5 tabular-nums chrome-text-muted">
                          {{ t('lineage.semantic_source_files', { count: tgt.source_files.length }) }}
                        </td>
                      </tr>
                    </tbody>
                  </table>
                </div>
              </div>
            </div>
          </div>
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
.lineage-edge-group {
  cursor: pointer;
}
.lineage-edge-group:hover .lineage-edge {
  stroke: rgb(var(--accent));
  opacity: 1;
}
/* 图内定位命中环(amber 虚线,盖在节点框上) */
.lineage-node-locate-ring {
  fill: none;
  stroke: rgb(245 158 11);
  stroke-width: 2;
  stroke-dasharray: 4 3;
  pointer-events: none;
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
