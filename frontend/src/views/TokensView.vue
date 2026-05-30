<script setup lang="ts">
/**
 * TokensView —— Figma Make TokensView 的 Vue 镜像。
 *
 * 7 个 design anchor 分组(PRD docs/prd/frontend-ui-v2.md Appendix A):
 *   §1 6 status dots
 *   §2 AI 4 级敏感度 (L1 / L2 / L3 / L4)
 *   §3 Compare 4 桶 (only-source / only-target / diff / same)
 *   §4 R7 工作流 10 节点白名单
 *   §5 License 5 状态
 *   §6 6 aspect tag
 *   §7 数据流方向:A 垂直 vs B 水平箭头
 *
 * 活的 design reference —— UI 团队改 token / 跨页色系前看这里。
 * 不接业务接口、不带 auth、不进 chrome nav。
 */
import {
  CheckCircle2,
  XCircle,
  Pause,
  AlertTriangle,
  Clock,
  ShieldCheck,
  ShieldAlert,
  Shield,
  ShieldX,
  ArrowRight,
  ArrowDown,
  Code2,
  Database,
  ListChecks,
  User,
  FolderKanban,
  Settings,
  Sparkles,
  GitMerge,
  Split,
  Workflow,
  Network,
  Filter as FilterIcon,
  Mail,
  Webhook,
  Timer,
  Boxes,
} from 'lucide-vue-next'
import { storeToRefs } from 'pinia'
import { useThemeStore } from '../stores/theme'
import { variants } from '../variants'

const themeStore = useThemeStore()
const { variant } = storeToRefs(themeStore)

const STATUS_DOTS = [
  { name: 'success', label: '成功', color: 'bg-emerald-500', desc: '操作终态、连接 OK' },
  { name: 'running', label: '进行中', color: 'bg-sky-500 animate-pulse', desc: 'job pending / running' },
  { name: 'failed', label: '失败', color: 'bg-red-500', desc: '需要用户介入' },
  { name: 'warning', label: '警告', color: 'bg-amber-500', desc: '可继续但要注意' },
  { name: 'cancelled', label: '已取消', color: 'bg-slate-400', desc: '用户主动 cancel' },
  { name: 'idle', label: '空闲', color: 'bg-slate-300 dark:bg-slate-600', desc: '未触发 / 草稿' },
]

const AI_LEVELS = [
  { id: 'L1', label: '元数据', icon: ShieldCheck, color: 'bg-emerald-50 text-emerald-700 dark:bg-emerald-500/15 dark:text-emerald-300 border-emerald-200/60 dark:border-emerald-500/30', desc: '表名 / 列名 / 注释,默认允许' },
  { id: 'L2', label: 'SQL 文本', icon: Shield, color: 'bg-sky-50 text-sky-700 dark:bg-sky-500/15 dark:text-sky-300 border-sky-200/60 dark:border-sky-500/30', desc: '查询语句,默认允许、记录审计' },
  { id: 'L3', label: '脱敏数据', icon: ShieldAlert, color: 'bg-amber-50 text-amber-700 dark:bg-amber-500/15 dark:text-amber-300 border-amber-200/60 dark:border-amber-500/30', desc: '行抽样,经脱敏管道' },
  { id: 'L4', label: '原始数据', icon: ShieldX, color: 'bg-red-50 text-red-700 dark:bg-red-500/15 dark:text-red-300 border-red-200/60 dark:border-red-500/30', desc: '私有部署默认锁,需 admin 显式解锁' },
]

const COMPARE_BUCKETS = [
  { id: 'only_source', label: '仅源', color: 'bg-emerald-500', textColor: 'text-emerald-700 dark:text-emerald-300', count: 12, desc: '目标缺失,候选 INSERT' },
  { id: 'only_target', label: '仅目标', color: 'bg-red-500', textColor: 'text-red-700 dark:text-red-300', count: 7, desc: '源缺失,候选 DELETE' },
  { id: 'diff', label: '差异', color: 'bg-amber-500', textColor: 'text-amber-700 dark:text-amber-300', count: 23, desc: '主键同、字段不同,候选 UPDATE' },
  { id: 'same', label: '一致', color: 'bg-slate-400 dark:bg-slate-500', textColor: 'text-slate-500 dark:text-slate-400', count: 1284, desc: '完全匹配' },
]

const WORKFLOW_NODES = [
  { id: 'start', label: 'Start', icon: Sparkles },
  { id: 'sql', label: 'SQL', icon: Code2 },
  { id: 'compare', label: 'Compare', icon: GitMerge },
  { id: 'branch', label: 'Branch', icon: Split },
  { id: 'merge', label: 'Merge', icon: Network },
  { id: 'filter', label: 'Filter', icon: FilterIcon },
  { id: 'notify', label: 'Notify', icon: Mail },
  { id: 'webhook', label: 'Webhook', icon: Webhook },
  { id: 'wait', label: 'Wait', icon: Timer },
  { id: 'end', label: 'End', icon: Workflow },
]

const LICENSE_STATES = [
  { id: 'active', label: 'Active', color: 'bg-emerald-50 text-emerald-700 dark:bg-emerald-500/15 dark:text-emerald-300 border-emerald-200/60 dark:border-emerald-500/30', icon: CheckCircle2, desc: '有效期内,所有功能解锁' },
  { id: 'trial', label: 'Trial', color: 'bg-sky-50 text-sky-700 dark:bg-sky-500/15 dark:text-sky-300 border-sky-200/60 dark:border-sky-500/30', icon: Clock, desc: '试用中,剩余 N 天' },
  { id: 'grace', label: 'Grace', color: 'bg-amber-50 text-amber-700 dark:bg-amber-500/15 dark:text-amber-300 border-amber-200/60 dark:border-amber-500/30', icon: AlertTriangle, desc: '过期宽限期,N 天后锁定' },
  { id: 'expired', label: 'Expired', color: 'bg-red-50 text-red-700 dark:bg-red-500/15 dark:text-red-300 border-red-200/60 dark:border-red-500/30', icon: XCircle, desc: '已过期,只读模式' },
  { id: 'paused', label: 'Paused', color: 'bg-slate-50 text-slate-600 dark:bg-slate-500/15 dark:text-slate-300 border-slate-200/60 dark:border-slate-500/30', icon: Pause, desc: 'admin 主动暂停' },
]

const ASPECT_TAGS = [
  { id: 'code', label: '代码', icon: Code2, color: 'bg-sky-50 text-sky-700 dark:bg-sky-500/15 dark:text-sky-300' },
  { id: 'data', label: '数据', icon: Database, color: 'bg-indigo-50 text-indigo-700 dark:bg-indigo-500/15 dark:text-indigo-300' },
  { id: 'task', label: '任务', icon: ListChecks, color: 'bg-amber-50 text-amber-700 dark:bg-amber-500/15 dark:text-amber-300' },
  { id: 'user', label: '用户', icon: User, color: 'bg-emerald-50 text-emerald-700 dark:bg-emerald-500/15 dark:text-emerald-300' },
  { id: 'project', label: '项目', icon: FolderKanban, color: 'bg-violet-50 text-violet-700 dark:bg-violet-500/15 dark:text-violet-300' },
  { id: 'system', label: '系统', icon: Settings, color: 'bg-slate-100 text-slate-700 dark:bg-slate-500/15 dark:text-slate-300' },
]
</script>

<template>
  <div class="min-h-full flex-1 chrome-bg-main">
    <div class="max-w-6xl mx-auto px-6 lg:px-10 py-10">
      <!-- 顶部说明 -->
      <div class="mb-10">
        <div class="text-xs uppercase tracking-wider chrome-text-muted font-medium font-mono">
          design · anchors · v2.0
        </div>
        <h1 class="text-h2 chrome-text-heading mt-1">活的 design reference</h1>
        <p class="chrome-text-muted text-sm mt-2 max-w-2xl leading-relaxed">
          16 个跨页视觉锚点,定义"成功红绿、AI 敏感度、Compare 四桶"等 token。UI
          团队改 token 时先来这里;每个 variant
          (<span class="font-mono chrome-accent">{{ variants.find((v) => v.id === variant)?.name }}</span>)
          切换时,本页同步重渲。
        </p>
      </div>

      <!-- §1 Status dots -->
      <section class="mb-12">
        <div class="flex items-baseline justify-between mb-4 border-b chrome-border-subtle pb-2">
          <div class="flex items-baseline gap-3">
            <span class="text-sm font-mono chrome-text-muted">§1</span>
            <h2 class="text-section font-semibold chrome-text-heading">状态点 · Status Dots</h2>
          </div>
          <span class="text-xs chrome-text-muted hidden md:inline">6 状态,跨页统一</span>
        </div>
        <div class="grid grid-cols-2 md:grid-cols-3 gap-3">
          <div
            v-for="s in STATUS_DOTS"
            :key="s.name"
            class="chrome-bg-panel border chrome-border rounded-card p-3 flex items-center gap-3"
          >
            <span class="w-2.5 h-2.5 rounded-full shrink-0" :class="s.color" />
            <div class="min-w-0">
              <div class="text-sm font-medium chrome-text-heading">{{ s.label }}</div>
              <div class="text-xs chrome-text-muted truncate" :title="s.desc">{{ s.desc }}</div>
            </div>
          </div>
        </div>
      </section>

      <!-- §2 AI 敏感度 -->
      <section class="mb-12">
        <div class="flex items-baseline justify-between mb-4 border-b chrome-border-subtle pb-2">
          <div class="flex items-baseline gap-3">
            <span class="text-sm font-mono chrome-text-muted">§2</span>
            <h2 class="text-section font-semibold chrome-text-heading">AI 敏感度 · 4 级</h2>
          </div>
          <span class="text-xs chrome-text-muted hidden md:inline">L4 私有部署默认锁</span>
        </div>
        <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-3">
          <div
            v-for="lv in AI_LEVELS"
            :key="lv.id"
            class="chrome-bg-panel border chrome-border rounded-card p-4"
          >
            <div class="flex items-center gap-2 mb-2">
              <span
                class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-input text-xs font-mono font-semibold border"
                :class="lv.color"
              >
                <component :is="lv.icon" class="w-3 h-3" />
                {{ lv.id }}
              </span>
              <span class="text-sm font-medium chrome-text-heading">{{ lv.label }}</span>
            </div>
            <div class="text-xs chrome-text-muted leading-relaxed">{{ lv.desc }}</div>
          </div>
        </div>
      </section>

      <!-- §3 Compare 4 桶 -->
      <section class="mb-12">
        <div class="flex items-baseline justify-between mb-4 border-b chrome-border-subtle pb-2">
          <div class="flex items-baseline gap-3">
            <span class="text-sm font-mono chrome-text-muted">§3</span>
            <h2 class="text-section font-semibold chrome-text-heading">Compare · 4 桶</h2>
          </div>
          <span class="text-xs chrome-text-muted hidden md:inline">
            emerald / red / amber / slate,严格四分,不允许二色合并
          </span>
        </div>
        <div class="grid grid-cols-2 lg:grid-cols-4 gap-3">
          <div
            v-for="b in COMPARE_BUCKETS"
            :key="b.id"
            class="chrome-bg-panel border chrome-border rounded-card p-4"
          >
            <div class="flex items-end justify-between mb-1">
              <span class="w-3 h-3 rounded-sm" :class="b.color" />
              <span class="text-2xl font-semibold tabular-nums chrome-text-heading">
                {{ b.count.toLocaleString() }}
              </span>
            </div>
            <div class="text-sm font-medium" :class="b.textColor">{{ b.label }}</div>
            <div class="text-xs chrome-text-muted mt-0.5">{{ b.desc }}</div>
          </div>
        </div>
      </section>

      <!-- §4 R7 工作流节点白名单 -->
      <section class="mb-12">
        <div class="flex items-baseline justify-between mb-4 border-b chrome-border-subtle pb-2">
          <div class="flex items-baseline gap-3">
            <span class="text-sm font-mono chrome-text-muted">§4</span>
            <h2 class="text-section font-semibold chrome-text-heading">R7 工作流节点白名单</h2>
          </div>
          <span class="text-xs chrome-text-muted hidden md:inline">
            10 种,业务前端只渲染白名单
          </span>
        </div>
        <div class="chrome-bg-panel border chrome-border rounded-card p-4">
          <div class="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-5 gap-2">
            <div
              v-for="n in WORKFLOW_NODES"
              :key="n.id"
              class="flex items-center gap-2 px-2.5 py-1.5 rounded-input border chrome-border chrome-bg-elevated"
            >
              <component :is="n.icon" class="w-3.5 h-3.5 chrome-accent" />
              <span class="text-xs font-mono chrome-text-normal">{{ n.label }}</span>
            </div>
          </div>
        </div>
      </section>

      <!-- §5 License -->
      <section class="mb-12">
        <div class="flex items-baseline justify-between mb-4 border-b chrome-border-subtle pb-2">
          <div class="flex items-baseline gap-3">
            <span class="text-sm font-mono chrome-text-muted">§5</span>
            <h2 class="text-section font-semibold chrome-text-heading">License · 5 状态</h2>
          </div>
          <span class="text-xs chrome-text-muted hidden md:inline">
            active / trial / grace / expired / paused
          </span>
        </div>
        <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-5 gap-3">
          <div
            v-for="l in LICENSE_STATES"
            :key="l.id"
            class="chrome-bg-panel border chrome-border rounded-card p-3"
          >
            <span
              class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-input text-xs font-medium border"
              :class="l.color"
            >
              <component :is="l.icon" class="w-3 h-3" />
              {{ l.label }}
            </span>
            <div class="text-xs chrome-text-muted mt-2 leading-snug">{{ l.desc }}</div>
          </div>
        </div>
      </section>

      <!-- §6 Aspect tags -->
      <section class="mb-12">
        <div class="flex items-baseline justify-between mb-4 border-b chrome-border-subtle pb-2">
          <div class="flex items-baseline gap-3">
            <span class="text-sm font-mono chrome-text-muted">§6</span>
            <h2 class="text-section font-semibold chrome-text-heading">资源 aspect tag · 6 类</h2>
          </div>
          <span class="text-xs chrome-text-muted hidden md:inline">
            Audit / 历史搜索 / Activity 按此 6 维过滤
          </span>
        </div>
        <div class="flex flex-wrap gap-2">
          <span
            v-for="a in ASPECT_TAGS"
            :key="a.id"
            class="inline-flex items-center gap-1 px-2 py-1 rounded-input text-xs font-medium"
            :class="a.color"
          >
            <component :is="a.icon" class="w-3 h-3" />
            {{ a.label }}
          </span>
        </div>
      </section>

      <!-- §7 A vs B 数据流方向 -->
      <section class="mb-12">
        <div class="flex items-baseline justify-between mb-4 border-b chrome-border-subtle pb-2">
          <div class="flex items-baseline gap-3">
            <span class="text-sm font-mono chrome-text-muted">§7</span>
            <h2 class="text-section font-semibold chrome-text-heading">数据流方向 · A vs B</h2>
          </div>
          <span class="text-xs chrome-text-muted hidden md:inline">
            A=查询结果纵向;B=lineage 横向
          </span>
        </div>
        <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div class="chrome-bg-panel border chrome-border rounded-card p-5">
            <div class="text-xs uppercase tracking-wider chrome-text-muted font-medium mb-3">
              A · 查询结果(纵向)
            </div>
            <div class="flex flex-col items-center gap-1">
              <div class="px-3 py-1.5 rounded-input border chrome-border chrome-bg-elevated text-xs font-mono chrome-text-normal">
                SELECT *
              </div>
              <ArrowDown class="w-4 h-4 chrome-accent" />
              <div class="px-3 py-1.5 rounded-input border chrome-border chrome-bg-elevated text-xs font-mono chrome-text-normal">
                row 1
              </div>
              <ArrowDown class="w-4 h-4 chrome-accent" />
              <div class="px-3 py-1.5 rounded-input border chrome-border chrome-bg-elevated text-xs font-mono chrome-text-normal">
                row 2
              </div>
              <ArrowDown class="w-4 h-4 chrome-text-muted" />
              <div class="text-xs chrome-text-muted font-mono">…</div>
            </div>
          </div>
          <div class="chrome-bg-panel border chrome-border rounded-card p-5">
            <div class="text-xs uppercase tracking-wider chrome-text-muted font-medium mb-3">
              B · Lineage(横向)
            </div>
            <div class="flex items-center justify-between gap-2">
              <div class="flex flex-col items-center gap-1">
                <Database class="w-5 h-5 chrome-accent" />
                <span class="text-xs font-mono chrome-text-normal">src</span>
              </div>
              <ArrowRight class="w-4 h-4 chrome-accent shrink-0" />
              <div class="flex flex-col items-center gap-1">
                <Boxes class="w-5 h-5 chrome-accent" />
                <span class="text-xs font-mono chrome-text-normal">trans</span>
              </div>
              <ArrowRight class="w-4 h-4 chrome-accent shrink-0" />
              <div class="flex flex-col items-center gap-1">
                <Database class="w-5 h-5 chrome-accent" />
                <span class="text-xs font-mono chrome-text-normal">tgt</span>
              </div>
            </div>
            <div class="text-xs chrome-text-muted mt-3 text-center">
              资源 / 数据流横向展开
            </div>
          </div>
        </div>
      </section>

      <div class="text-center text-xs chrome-text-muted font-mono mt-12 pb-6">
        TokensView · mirror of Figma Make · {{ variants.find((v) => v.id === variant)?.name }}
      </div>
    </div>
  </div>
</template>
