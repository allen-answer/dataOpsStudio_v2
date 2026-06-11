<script setup lang="ts">
/**
 * Design System Preview — T7 第一阶段产物。
 *
 * 把契约 §10 的全部 token / 密度规则 / signature 渐变排成一页样品,
 * 不接后端,纯视觉。人在浏览器看效果后调 token,定版再做业务页面。
 */
import { ref } from 'vue'
import {
  Database,
  LayoutGrid,
  Terminal,
  ListChecks,
  Settings,
  Plus,
  Trash2,
  Search,
  ChevronRight,
  Sparkles,
  Activity,
  CheckCircle2,
  CircleDot,
} from 'lucide-vue-next'
import SectionTitle from '../components/SectionTitle.vue'
import Swatch from '../components/Swatch.vue'
import StatusDot from '../components/StatusDot.vue'
import StatusItem from '../components/StatusItem.vue'

type Status = 'pending' | 'running' | 'success' | 'failed' | 'cancelled'

const sections = [
  { id: 'colors', label: '色彩' },
  { id: 'typography', label: '字体' },
  { id: 'buttons', label: '按钮' },
  { id: 'inputs', label: '输入框' },
  { id: 'status', label: '状态点' },
  { id: 'shell-vs-work', label: '外壳 vs 工作区' },
  { id: 'nav', label: '导航' },
  { id: 'workspace-dark', label: '深色工作区' },
  { id: 'signature', label: '签名渐变' },
]

const focusInput = ref('focus_demo')
const errorInput = ref('not-an-email')

// 致密表格示例数据 —— 模拟 GET /api/jobs 返回(无真实 API)
const jobRows: {
  id: string
  kind: string
  status: Status
  started: string
  elapsed_ms: number | null
  rows: number | null
}[] = [
  { id: 'job_8f2e1c4a', kind: 'sql_query', status: 'success', started: '14:32:01', elapsed_ms: 1426, rows: 1 },
  { id: 'job_a17d92bb', kind: 'sql_query', status: 'running', started: '14:33:55', elapsed_ms: 2840, rows: null },
  { id: 'job_3c5b81e9', kind: 'test_connection', status: 'success', started: '14:31:18', elapsed_ms: 119, rows: null },
  { id: 'job_7e29ab44', kind: 'sql_query', status: 'failed', started: '14:28:42', elapsed_ms: 3201, rows: null },
  { id: 'job_b94f1d20', kind: 'sql_query', status: 'cancelled', started: '14:25:09', elapsed_ms: 5860, rows: null },
  { id: 'job_2dc6e88a', kind: 'test_connection', status: 'pending', started: '—', elapsed_ms: null, rows: null },
]

const navItems = [
  { icon: LayoutGrid, label: '项目', active: false },
  { icon: Database, label: '数据源', active: true },
  { icon: Terminal, label: 'SQL', active: false },
  { icon: ListChecks, label: '任务', active: false },
  { icon: Settings, label: '设置', active: false },
]

const statusLabel = (s: Status): string =>
  ({
    pending: 'Pending',
    running: 'Running',
    success: 'Success',
    failed: 'Failed',
    cancelled: 'Cancelled',
  })[s]

const darkResultRows: [string, number][] = [
  ['u_4f8a2', 1432],
  ['u_9b1c7', 1287],
  ['u_2e6d3', 1101],
  ['u_7a5f8', 982],
  ['u_3c9e1', 876],
]
</script>

<template>
  <div class="min-h-screen flex">
    <!-- ─── 左侧 jumplinks rail(本页自身导航,非样品)─── -->
    <aside
      class="hidden lg:block w-44 shrink-0 border-r border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 sticky top-[52px] self-start h-[calc(100vh-52px)] overflow-y-auto"
    >
      <div class="px-4 pt-5 pb-3 border-b border-slate-100 dark:border-slate-800">
        <div class="flex items-center gap-2">
          <div class="w-7 h-7 rounded-card bg-sky-gradient grid place-items-center shadow-subtle">
            <Sparkles class="w-4 h-4 text-white" />
          </div>
          <div class="text-section font-medium tracking-tight">DataOps</div>
        </div>
        <div class="text-xs text-slate-500 dark:text-slate-400 mt-1 ml-9">Design System</div>
      </div>
      <nav class="py-2 text-ui">
        <a
          v-for="s in sections"
          :key="s.id"
          :href="`#${s.id}`"
          class="flex items-center justify-between px-4 py-2 text-slate-600 dark:text-slate-400 hover:text-sky-600 dark:hover:text-sky-400 hover:bg-sky-50 dark:hover:bg-sky-500/10 transition-colors"
        >
          <span>{{ s.label }}</span>
          <ChevronRight class="w-3.5 h-3.5 text-slate-300 dark:text-slate-600" />
        </a>
      </nav>
    </aside>

    <!-- ─── 主内容 ─── -->
    <main class="flex-1 max-w-6xl mx-auto px-6 lg:px-10 py-10 space-y-16">
      <!-- Header -->
      <header>
        <div class="text-xs text-slate-500 dark:text-slate-400 uppercase tracking-wider font-medium">T7 · Phase 1</div>
        <h1 class="text-h2 font-semibold mt-1 tracking-tight">DataOps Studio · Design System</h1>
        <p class="text-slate-500 dark:text-slate-400 mt-2 max-w-2xl leading-relaxed">
          契约 §10 的 token 全部样品。<em class="text-slate-700 dark:text-slate-200 not-italic font-medium">清爽外壳 + 致密工作区</em>,
          天空蓝只做强调色。看完调 token,定版再做业务页。
        </p>
      </header>

      <!-- ─── §1 色彩 ─── -->
      <section id="colors" class="space-y-4">
        <SectionTitle :index="1" title="色彩 Colors" hint="主色仅用于强调,大面积铺底见 slate-50 / white" />
        <div class="grid grid-cols-2 md:grid-cols-4 gap-3">
          <Swatch color="bg-sky-500" name="sky-500" hex="#0EA5E9" usage="主按钮 / 选中 / 链接 / focus" />
          <Swatch color="bg-sky-600" name="sky-600" hex="#0284C7" usage="主色 hover" />
          <Swatch color="bg-sky-50" name="sky-50" hex="#F0F9FF" usage="选中行 / 浅强调" />
          <Swatch color="bg-slate-50" name="slate-50" hex="#F8FAFC" usage="外壳背景" />
          <Swatch color="bg-white border border-slate-200 dark:border-slate-700" name="white" hex="#FFFFFF" usage="卡片底" />
          <Swatch color="bg-slate-200" name="slate-200" hex="#E2E8F0" usage="边框 / 分隔" />
          <Swatch color="bg-slate-500" name="slate-500" hex="#64748B" usage="次要文字" />
          <Swatch color="bg-slate-800" name="slate-800" hex="#1E293B" usage="主要文字(非纯黑)" />
        </div>
        <div class="grid grid-cols-2 md:grid-cols-4 gap-3 pt-2">
          <Swatch color="bg-emerald-500" name="emerald-500" hex="#10B981" usage="成功 / success" />
          <Swatch color="bg-red-500" name="red-500" hex="#EF4444" usage="危险 / failed" />
          <Swatch color="bg-amber-500" name="amber-500" hex="#F59E0B" usage="警告 / warning" />
          <Swatch color="bg-slate-900" name="slate-900" hex="#0F172A" usage="工作区深底(可选)" />
        </div>
      </section>

      <!-- ─── §2 字体 ─── -->
      <section id="typography" class="space-y-4">
        <SectionTitle :index="2" title="字体 Typography" hint="界面用系统字体,代码/数据用等宽" />
        <div class="grid md:grid-cols-2 gap-4">
          <div class="bg-white dark:bg-slate-800/60 border border-slate-200 dark:border-slate-700 rounded-card p-6 shadow-subtle">
            <div class="text-xs uppercase tracking-wider text-slate-500 dark:text-slate-400 font-medium mb-3">界面 · 系统字体</div>
            <div class="space-y-2 font-sans">
              <div class="text-h2 font-semibold tracking-tight text-slate-800 dark:text-slate-100">数据工作台,从这里开始</div>
              <div class="text-section font-medium text-slate-800 dark:text-slate-100">DataOps Studio 2.0</div>
              <div class="text-ui text-slate-800 dark:text-slate-100">正文 14px · 用户能舒服地看 20 分钟。</div>
              <div class="text-sm text-slate-500 dark:text-slate-400">次要信息 · slate-500 · 12px</div>
            </div>
          </div>
          <div class="bg-white dark:bg-slate-800/60 border border-slate-200 dark:border-slate-700 rounded-card p-6 shadow-subtle">
            <div class="text-xs uppercase tracking-wider text-slate-500 dark:text-slate-400 font-medium mb-3">代码 / 数据 · 等宽</div>
            <div class="space-y-2 font-mono text-data text-slate-800 dark:text-slate-100">
              <div>SELECT id, name FROM users;</div>
              <div class="tabular-nums">1,234,567.89  →  987,654.32</div>
              <div class="text-slate-500 dark:text-slate-400">-- 等宽数字,列对齐用</div>
              <div class="text-slate-500 dark:text-slate-400">job_8f2e1c4a · 14:32:01 · 1.4s</div>
            </div>
          </div>
        </div>
      </section>

      <!-- ─── §3 按钮 ─── -->
      <section id="buttons" class="space-y-4">
        <SectionTitle :index="3" title="按钮 Buttons" hint="主按钮带 sky-400→sky-600 微渐变;其他纯色克制" />
        <div class="bg-white dark:bg-slate-800/60 border border-slate-200 dark:border-slate-700 rounded-card p-6 shadow-subtle space-y-6">
          <div class="space-y-2">
            <div class="text-xs uppercase tracking-wider text-slate-500 dark:text-slate-400 font-medium">主按钮 · 渐变签名色</div>
            <div class="flex flex-wrap items-center gap-3">
              <button class="btn-primary">
                <Plus class="w-4 h-4" />
                新建数据源
              </button>
              <button class="btn-primary btn-primary-hover">
                <span>hover 态(深一档)</span>
              </button>
              <button class="btn-primary opacity-50 cursor-not-allowed">禁用</button>
            </div>
          </div>
          <div class="space-y-2">
            <div class="text-xs uppercase tracking-wider text-slate-500 dark:text-slate-400 font-medium">次按钮 · 边框 + 纯色</div>
            <div class="flex flex-wrap items-center gap-3">
              <button class="btn-secondary">取消</button>
              <button class="btn-secondary text-sky-600 dark:text-sky-400 border-sky-200 dark:border-sky-500/30 hover:bg-sky-50 dark:hover:bg-sky-500/10">
                查看详情
                <ChevronRight class="w-3.5 h-3.5" />
              </button>
              <button class="btn-secondary opacity-50 cursor-not-allowed">禁用</button>
            </div>
          </div>
          <div class="space-y-2">
            <div class="text-xs uppercase tracking-wider text-slate-500 dark:text-slate-400 font-medium">危险按钮 · red-500</div>
            <div class="flex flex-wrap items-center gap-3">
              <button class="btn-danger">
                <Trash2 class="w-4 h-4" />
                删除数据源
              </button>
              <button class="btn-danger-outline">
                <Trash2 class="w-4 h-4" />
                危险次态
              </button>
              <button class="btn-danger opacity-50 cursor-not-allowed">禁用</button>
            </div>
          </div>
          <div class="space-y-2">
            <div class="text-xs uppercase tracking-wider text-slate-500 dark:text-slate-400 font-medium">尺寸</div>
            <div class="flex flex-wrap items-center gap-3">
              <button class="btn-primary btn-sm">
                <Plus class="w-3.5 h-3.5" />
                小号
              </button>
              <button class="btn-primary">默认</button>
              <button class="btn-primary btn-lg">大号</button>
            </div>
          </div>
        </div>
      </section>

      <!-- ─── §4 输入框 ─── -->
      <section id="inputs" class="space-y-4">
        <SectionTitle :index="4" title="输入框 Inputs" hint="圆角 6px,focus 蓝边" />
        <div class="bg-white dark:bg-slate-800/60 border border-slate-200 dark:border-slate-700 rounded-card p-6 shadow-subtle grid md:grid-cols-2 gap-6">
          <div class="space-y-2">
            <label class="block text-xs uppercase tracking-wider text-slate-500 dark:text-slate-400 font-medium">普通</label>
            <input type="text" placeholder="warehouse-prod" class="input" />
          </div>
          <div class="space-y-2">
            <label class="block text-xs uppercase tracking-wider text-slate-500 dark:text-slate-400 font-medium">Focus 态(常驻显示)</label>
            <input v-model="focusInput" class="input input-focus" />
          </div>
          <div class="space-y-2">
            <label class="block text-xs uppercase tracking-wider text-slate-500 dark:text-slate-400 font-medium">错误</label>
            <input v-model="errorInput" class="input input-error" />
            <div class="text-xs text-red-500">不是合法的邮箱地址</div>
          </div>
          <div class="space-y-2">
            <label class="block text-xs uppercase tracking-wider text-slate-500 dark:text-slate-400 font-medium">禁用</label>
            <input type="text" value="只读" disabled class="input bg-slate-50 text-slate-500 dark:text-slate-400 cursor-not-allowed" />
          </div>
          <div class="space-y-2 md:col-span-2">
            <label class="block text-xs uppercase tracking-wider text-slate-500 dark:text-slate-400 font-medium">前置图标 · search</label>
            <div class="relative">
              <Search class="w-4 h-4 text-slate-400 dark:text-slate-500 absolute left-3 top-1/2 -translate-y-1/2" />
              <input type="text" placeholder="搜索数据源..." class="input pl-9" />
            </div>
          </div>
        </div>
      </section>

      <!-- ─── §5 状态点 ─── -->
      <section id="status" class="space-y-4">
        <SectionTitle :index="5" title="状态点 Status Dots" hint="job 状态用圆点+文字,不用大色块" />
        <div class="bg-white dark:bg-slate-800/60 border border-slate-200 dark:border-slate-700 rounded-card p-6 shadow-subtle">
          <div class="grid grid-cols-2 md:grid-cols-5 gap-4">
            <StatusItem status="pending" />
            <StatusItem status="running" />
            <StatusItem status="success" />
            <StatusItem status="failed" />
            <StatusItem status="cancelled" />
          </div>
          <div class="mt-6 pt-6 border-t border-slate-100 dark:border-slate-800">
            <div class="text-xs uppercase tracking-wider text-slate-500 dark:text-slate-400 font-medium mb-3">在列表里(就近预览)</div>
            <div class="space-y-1.5 font-mono text-data">
              <div class="flex items-center gap-3 py-1.5">
                <StatusDot status="running" />
                <span class="text-slate-800 dark:text-slate-100">job_a17d92bb</span>
                <span class="text-slate-500 dark:text-slate-400">SELECT * FROM events WHERE ...</span>
              </div>
              <div class="flex items-center gap-3 py-1.5">
                <StatusDot status="success" />
                <span class="text-slate-800 dark:text-slate-100">job_8f2e1c4a</span>
                <span class="text-slate-500 dark:text-slate-400">1.4s · 1 row</span>
              </div>
              <div class="flex items-center gap-3 py-1.5">
                <StatusDot status="failed" />
                <span class="text-slate-800 dark:text-slate-100">job_7e29ab44</span>
                <span class="text-slate-500 dark:text-slate-400">sql_execution_failed</span>
              </div>
            </div>
          </div>
        </div>
      </section>

      <!-- ─── §6 外壳 vs 工作区(密度对比,§10 核心)─── -->
      <section id="shell-vs-work" class="space-y-4">
        <SectionTitle
          :index="6"
          title="外壳 vs 工作区 · 密度对比"
          hint="§10 的核心拍板 —— 外壳留白舒展,工作区信息密度优先"
        />
        <div class="grid lg:grid-cols-2 gap-4">
          <!-- 外壳样品:舒展卡片 -->
          <div>
            <div class="text-xs uppercase tracking-wider text-slate-500 dark:text-slate-400 font-medium mb-2 flex items-center gap-2">
              <CircleDot class="w-3 h-3 text-sky-500" />
              外壳 · 舒展
            </div>
            <div class="bg-white dark:bg-slate-800/60 border border-slate-200 dark:border-slate-700 rounded-card shadow-subtle data-shell-pad">
              <div class="space-y-6">
                <div>
                  <div class="text-section font-semibold tracking-tight">创建数据源</div>
                  <div class="text-slate-500 dark:text-slate-400 mt-1">配置一个 MySQL / PostgreSQL 连接</div>
                </div>
                <div class="space-y-4">
                  <div class="space-y-2">
                    <label class="block text-xs uppercase tracking-wider text-slate-500 dark:text-slate-400 font-medium">名称</label>
                    <input class="input" placeholder="warehouse-prod" />
                  </div>
                  <div class="space-y-2">
                    <label class="block text-xs uppercase tracking-wider text-slate-500 dark:text-slate-400 font-medium">主机</label>
                    <input class="input" placeholder="127.0.0.1" />
                  </div>
                </div>
                <div class="flex gap-3 pt-2">
                  <button class="btn-primary">
                    <Plus class="w-4 h-4" />
                    创建
                  </button>
                  <button class="btn-secondary">取消</button>
                </div>
              </div>
            </div>
          </div>

          <!-- 工作区样品:致密表格 -->
          <div>
            <div class="text-xs uppercase tracking-wider text-slate-500 dark:text-slate-400 font-medium mb-2 flex items-center gap-2">
              <Activity class="w-3 h-3 text-sky-500" />
              工作区 · 致密
            </div>
            <div class="bg-white dark:bg-slate-800/60 border border-slate-200 dark:border-slate-700 rounded-card shadow-subtle overflow-hidden">
              <div class="px-3 py-2 border-b border-slate-200 dark:border-slate-700 flex items-center gap-2 bg-slate-50/60 dark:bg-slate-900/40">
                <ListChecks class="w-3.5 h-3.5 text-slate-500 dark:text-slate-400" />
                <span class="text-xs font-medium text-slate-700 dark:text-slate-200">任务列表 · 13px 等宽</span>
                <span class="ml-auto text-xs text-slate-400 dark:text-slate-500 tabular-nums">{{ jobRows.length }} jobs</span>
              </div>
              <div class="max-h-[420px] overflow-y-auto">
                <table class="w-full text-data">
                  <thead class="sticky top-0 z-10 bg-white dark:bg-slate-800">
                    <tr class="text-left text-xs text-slate-500 dark:text-slate-400 border-b border-slate-200 dark:border-slate-700">
                      <th class="font-medium py-2 px-3">Status</th>
                      <th class="font-medium py-2 px-3">Job ID</th>
                      <th class="font-medium py-2 px-3">Kind</th>
                      <th class="font-medium py-2 px-3">Started</th>
                      <th class="font-medium py-2 px-3 text-right">Elapsed</th>
                      <th class="font-medium py-2 px-3 text-right">Rows</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr
                      v-for="(job, i) in jobRows"
                      :key="job.id"
                      class="border-b border-slate-100 dark:border-slate-800 last:border-b-0 data-work-row"
                      :class="i % 2 ? 'bg-slate-50/40 dark:bg-slate-800/30' : ''"
                    >
                      <td>
                        <div class="flex items-center gap-2">
                          <StatusDot :status="job.status" />
                          <span class="text-xs text-slate-700 dark:text-slate-200">{{ statusLabel(job.status) }}</span>
                        </div>
                      </td>
                      <td class="font-mono text-slate-800 dark:text-slate-100">{{ job.id }}</td>
                      <td class="text-slate-500 dark:text-slate-400">{{ job.kind }}</td>
                      <td class="text-slate-500 dark:text-slate-400 tabular-nums">{{ job.started }}</td>
                      <td class="text-right tabular-nums text-slate-700 dark:text-slate-200">
                        {{ job.elapsed_ms !== null ? `${(job.elapsed_ms / 1000).toFixed(2)}s` : '—' }}
                      </td>
                      <td class="text-right tabular-nums text-slate-700 dark:text-slate-200">
                        {{ job.rows ?? '—' }}
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        </div>
        <p class="text-xs text-slate-500 dark:text-slate-400 leading-relaxed">
          注意对比:外壳卡片间距 16-24px,呼吸感强;工作区表格行高 ~28px、字号 13px、列对齐 tabular-nums、表头吸顶、斑马纹极淡(slate-50/40)、
          统一圆角和蓝强调点保持调性一致 —— 这就是 §10 说的"密度分区"。
        </p>
      </section>

      <!-- ─── §7 左侧窄导航 ─── -->
      <section id="nav" class="space-y-4">
        <SectionTitle :index="7" title="左侧窄导航 Nav Rail" hint="图标 + 文字,选中态天空蓝" />
        <div class="bg-white dark:bg-slate-800/60 border border-slate-200 dark:border-slate-700 rounded-card shadow-subtle overflow-hidden">
          <div class="flex h-[280px]">
            <!-- 样品 nav -->
            <nav class="w-16 border-r border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900/60 py-3 flex flex-col items-center gap-1">
              <div class="w-8 h-8 mb-3 rounded-card bg-sky-gradient grid place-items-center shadow-subtle">
                <Sparkles class="w-4 h-4 text-white" />
              </div>
              <button
                v-for="(item, i) in navItems"
                :key="i"
                class="w-12 py-2 rounded-card flex flex-col items-center gap-0.5 transition-colors relative"
                :class="
                  item.active
                    ? 'bg-sky-50 dark:bg-sky-500/15 text-sky-600 dark:text-sky-400'
                    : 'text-slate-500 dark:text-slate-400 hover:bg-slate-50 dark:hover:bg-slate-800/60 hover:text-slate-700 dark:hover:text-slate-200'
                "
              >
                <span
                  v-if="item.active"
                  class="absolute left-0 top-1.5 bottom-1.5 w-0.5 rounded-r bg-sky-500"
                ></span>
                <component :is="item.icon" class="w-4 h-4" />
                <span class="text-[10px] font-medium">{{ item.label }}</span>
              </button>
            </nav>

            <!-- 内容占位 -->
            <div class="flex-1 px-6 py-5 bg-slate-50/40 dark:bg-slate-800/30">
              <div class="text-xs uppercase tracking-wider text-slate-500 dark:text-slate-400 font-medium">数据源</div>
              <div class="text-section font-semibold mt-1 tracking-tight">warehouse-prod</div>
              <div class="text-slate-500 dark:text-slate-400 mt-1">MySQL · 127.0.0.1:3307 · 8 张表</div>
              <div class="mt-6 flex gap-2">
                <button class="btn-primary btn-sm">
                  <Plus class="w-3.5 h-3.5" />
                  新查询
                </button>
                <button class="btn-secondary btn-sm">测试连接</button>
              </div>
            </div>
          </div>
        </div>
      </section>

      <!-- ─── §8 深色工作区(SQL 编辑器)─── -->
      <section id="workspace-dark" class="space-y-4">
        <SectionTitle
          :index="8"
          title="深色工作区 Workspace Dark"
          hint="SQL 编辑器 / 结果区可选 slate-900,DBA 久看护眼"
        />
        <div class="rounded-card overflow-hidden shadow-subtle border border-slate-800">
          <!-- 工具条 -->
          <div class="bg-slate-900 border-b border-slate-800 px-4 py-2 flex items-center gap-3">
            <div class="flex gap-1.5">
              <div class="w-2.5 h-2.5 rounded-full bg-slate-700"></div>
              <div class="w-2.5 h-2.5 rounded-full bg-slate-700"></div>
              <div class="w-2.5 h-2.5 rounded-full bg-slate-700"></div>
            </div>
            <div class="text-xs text-slate-400 dark:text-slate-500 font-mono">query_1.sql · warehouse-prod</div>
            <div class="ml-auto flex items-center gap-2">
              <button class="text-xs px-2 py-1 rounded text-slate-300 hover:bg-slate-800 transition-colors">
                Cancel
              </button>
              <button class="text-xs px-3 py-1 rounded bg-sky-gradient text-white font-medium hover:opacity-90 transition-opacity">
                Run · ⌘↵
              </button>
            </div>
          </div>

          <!-- SQL 编辑器 mock -->
          <div class="bg-slate-900 text-data font-mono leading-relaxed py-3 px-4">
            <div class="flex">
              <div class="text-slate-600 select-none pr-4 text-right" style="min-width: 1.8rem">
                <div>1</div>
                <div>2</div>
                <div>3</div>
                <div>4</div>
                <div>5</div>
                <div>6</div>
              </div>
              <pre class="text-slate-200 m-0"><span class="text-slate-500 dark:text-slate-400">-- 最近 7 天 active users top 10</span>
<span class="text-sky-300">SELECT</span> user_id, <span class="text-sky-300">COUNT</span>(*) <span class="text-sky-300">AS</span> events
<span class="text-sky-300">FROM</span> events
<span class="text-sky-300">WHERE</span> ts &gt;= <span class="text-emerald-300">'2026-05-22'</span>
<span class="text-sky-300">GROUP BY</span> user_id
<span class="text-sky-300">ORDER BY</span> events <span class="text-sky-300">DESC</span> <span class="text-sky-300">LIMIT</span> <span class="text-amber-300">10</span>;</pre>
            </div>
          </div>

          <!-- 结果区 mock —— 同深色,但密度更紧 -->
          <div class="bg-slate-900 border-t border-slate-800">
            <div class="px-4 py-2 border-b border-slate-800 flex items-center gap-2 text-xs text-slate-400 dark:text-slate-500">
              <CheckCircle2 class="w-3.5 h-3.5 text-emerald-500" />
              <span class="font-mono">10 rows · 247ms · cached</span>
            </div>
            <table class="w-full text-data font-mono">
              <thead>
                <tr class="text-left text-slate-500 dark:text-slate-400 border-b border-slate-800">
                  <th class="font-medium py-1.5 px-4">user_id</th>
                  <th class="font-medium py-1.5 px-4 text-right">events</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="(row, i) in darkResultRows"
                  :key="i"
                  class="border-b border-slate-800/60 last:border-b-0"
                >
                  <td class="py-1 px-4 text-slate-300">{{ row[0] }}</td>
                  <td class="py-1 px-4 text-right tabular-nums text-slate-200">{{ row[1] }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </section>

      <!-- ─── §9 signature 渐变 ─── -->
      <section id="signature" class="space-y-4">
        <SectionTitle :index="9" title="签名渐变 Signature" hint="主按钮 / logo / 空状态用一点 sky-400→sky-600,其他纯色克制" />
        <div class="grid md:grid-cols-3 gap-4">
          <!-- 渐变按钮 -->
          <div class="bg-white dark:bg-slate-800/60 border border-slate-200 dark:border-slate-700 rounded-card p-6 shadow-subtle">
            <div class="text-xs uppercase tracking-wider text-slate-500 dark:text-slate-400 font-medium mb-4">主按钮</div>
            <button class="btn-primary">
              <Plus class="w-4 h-4" />
              新建数据源
            </button>
          </div>
          <!-- Logo -->
          <div class="bg-white dark:bg-slate-800/60 border border-slate-200 dark:border-slate-700 rounded-card p-6 shadow-subtle">
            <div class="text-xs uppercase tracking-wider text-slate-500 dark:text-slate-400 font-medium mb-4">品牌 Logo</div>
            <div class="flex items-center gap-3">
              <div class="w-10 h-10 rounded-card bg-sky-gradient grid place-items-center shadow-subtle">
                <Sparkles class="w-5 h-5 text-white" />
              </div>
              <div>
                <div class="text-section font-semibold tracking-tight">DataOps</div>
                <div class="text-xs text-slate-500 dark:text-slate-400">Studio 2.0</div>
              </div>
            </div>
          </div>
          <!-- 空状态 -->
          <div class="bg-white dark:bg-slate-800/60 border border-slate-200 dark:border-slate-700 rounded-card p-6 shadow-subtle">
            <div class="text-xs uppercase tracking-wider text-slate-500 dark:text-slate-400 font-medium mb-4">空状态</div>
            <div class="text-center py-2">
              <div class="w-12 h-12 mx-auto rounded-card bg-sky-gradient-soft grid place-items-center mb-3 border border-sky-100">
                <Database class="w-5 h-5 text-sky-500" />
              </div>
              <div class="text-sm text-slate-700 dark:text-slate-200 font-medium">还没有数据源</div>
              <div class="text-xs text-slate-500 dark:text-slate-400 mt-0.5">连接 MySQL 或 PostgreSQL 开始</div>
            </div>
          </div>
        </div>
        <p class="text-xs text-slate-500 dark:text-slate-400 leading-relaxed">
          §10.4 原话:"一点点渐变即可,多了俗"。当前页面里渐变只在 3 处出现:logo / 主按钮 / 空状态插画底框。
          其余强调全用纯 sky-500 / sky-50。
        </p>
      </section>

      <footer class="pt-6 border-t border-slate-200 dark:border-slate-700 text-xs text-slate-400 dark:text-slate-500 flex items-center justify-between">
        <div>DataOps Studio 2.0 · Design System Preview · T7 Phase 1</div>
        <div class="font-mono">§10 token defined in tailwind.config.ts</div>
      </footer>
    </main>
  </div>
</template>

<style scoped>
/* 按钮 / 输入框基础类 —— border-radius/gradient/shadow 走 CSS 变量,
   跟随 [data-variant=...] 切换。 */
.btn-primary,
.btn-secondary,
.btn-danger,
.btn-danger-outline {
  @apply inline-flex items-center justify-center gap-1.5 px-4 py-2 text-ui font-medium select-none whitespace-nowrap transition-all duration-150;
  border-radius: var(--radius-card);
}

.btn-primary {
  @apply text-white;
  background-image: var(--gradient-primary);
  box-shadow: var(--shadow-card);
}
.btn-primary:hover,
.btn-primary-hover {
  background-image: var(--gradient-primary-hover);
}

.btn-secondary {
  @apply bg-white border border-slate-200 dark:border-slate-700 text-slate-700 dark:text-slate-200;
}
.btn-secondary:hover {
  @apply bg-slate-50 border-slate-300;
}

.btn-danger {
  @apply bg-red-500 text-white;
  box-shadow: var(--shadow-card);
}
.btn-danger:hover {
  @apply bg-red-600;
}

.btn-danger-outline {
  @apply bg-white border border-red-200 text-red-600;
}
.btn-danger-outline:hover {
  @apply bg-red-50 border-red-300;
}

.btn-sm {
  @apply px-3 py-1.5 text-sm;
}
.btn-lg {
  @apply px-5 py-2.5 text-base;
}

.input {
  @apply w-full border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900/50 px-3 py-2 text-ui text-slate-800 dark:text-slate-100 placeholder:text-slate-400 dark:placeholder:text-slate-500 transition-colors;
  border-radius: var(--radius-input);
}
.input:focus,
.input-focus {
  @apply outline-none border-sky-500 dark:border-sky-400 ring-2 ring-sky-100 dark:ring-sky-500/20;
}
.input-error {
  @apply border-red-500;
  --tw-ring-color: rgb(254 226 226);
  box-shadow: 0 0 0 2px var(--tw-ring-color);
}
[data-mode='dark'] .input-error {
  --tw-ring-color: rgba(239, 68, 68, 0.2);
}

/* secondary 按钮在深色下:边/底/字都加深变种 */
[data-mode='dark'] .btn-secondary {
  @apply bg-slate-800 border-slate-700 text-slate-200;
}
[data-mode='dark'] .btn-secondary:hover {
  @apply bg-slate-700 border-slate-600;
}
[data-mode='dark'] .btn-danger-outline {
  @apply bg-transparent border-red-500/30 text-red-400;
}
[data-mode='dark'] .btn-danger-outline:hover {
  @apply bg-red-500/10 border-red-500/50;
}
</style>
