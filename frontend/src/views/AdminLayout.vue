<script setup lang="ts">
/**
 * AdminLayout —— /admin/* 外壳。
 *   - admin 顶部条(深色识别 admin 路由,附录 A #16)
 *   - 左侧 admin 导航(用户 / 项目 / License / 审计;AI 配置 / 调度 / Aspect 卡后端,disabled)
 *   - License 横条(D.4,复用全局组件)
 *
 * 守卫:router meta.admin=true → guards.ts 拦非 admin;此处再做一层 403 兜底呈现
 * (双层防御:前端守卫 + 真访问时后端 403)。
 */
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import {
  Users,
  FolderKanban,
  KeyRound,
  ScrollText,
  Bot,
  CalendarClock,
  Tags,
  ArrowLeft,
  ShieldCheck,
} from 'lucide-vue-next'
import { useAuthStore } from '../stores/auth'
import UserMenu from '../components/UserMenu.vue'
import LicenseBanner from '../components/LicenseBanner.vue'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const isAdmin = computed(() => auth.user?.role === 'admin')

interface AdminNavItem {
  name: string
  routeName?: string // 有 = 可达;无 = 卡后端 disabled
  icon: typeof Users
  blockedReason?: string
}

const items = computed<AdminNavItem[]>(() => [
  { name: 'users', routeName: 'admin-users', icon: Users },
  { name: 'projects', routeName: 'admin-projects', icon: FolderKanban },
  { name: 'license', routeName: 'admin-license', icon: KeyRound },
  { name: 'audit', routeName: 'admin-audit', icon: ScrollText },
  // ─── 卡后端:无端点,不建假页面(见 PR 描述)───
  { name: 'ai_config', icon: Bot, blockedReason: t('admin.nav_blocked_no_endpoint') },
  { name: 'scheduler', icon: CalendarClock, blockedReason: t('admin.nav_blocked_no_endpoint') },
  { name: 'aspects', icon: Tags, blockedReason: t('admin.nav_blocked_no_endpoint') },
])

function isActive(item: AdminNavItem): boolean {
  return item.routeName != null && route.name === item.routeName
}
function go(item: AdminNavItem): void {
  if (item.routeName) void router.push({ name: item.routeName })
}
function backToApp(): void {
  void router.push({ name: 'projects' })
}
</script>

<template>
  <!-- 非 admin 兜底(双层守卫第二层)-->
  <div
    v-if="!isAdmin"
    class="min-h-screen flex-1 grid place-items-center chrome-bg-main px-6"
  >
    <div class="max-w-md text-center">
      <div
        class="w-12 h-12 mx-auto rounded-card grid place-items-center bg-red-50 dark:bg-red-500/10 mb-4"
      >
        <ShieldCheck class="w-6 h-6 text-red-500" />
      </div>
      <h1 class="text-h2 font-semibold chrome-text-heading">{{ t('admin.forbidden_title') }}</h1>
      <p class="text-sm chrome-text-muted mt-2">{{ t('admin.forbidden_hint') }}</p>
      <button type="button" class="chrome-btn-secondary mt-5 mx-auto" @click="backToApp">
        <ArrowLeft class="w-4 h-4" />
        {{ t('admin.back_to_app') }}
      </button>
    </div>
  </div>

  <div v-else class="min-h-screen flex flex-1 chrome-bg-main">
    <!-- 左侧 admin 导航(宽于普通 NavRail,带文字)-->
    <nav
      class="w-52 shrink-0 border-r chrome-border flex flex-col py-3 px-2 gap-0.5"
      style="background-color: rgb(15 23 42); color: rgb(148 163 184);"
    >
      <div class="px-2.5 pb-3 mb-1 flex items-center gap-2 border-b border-slate-700/60">
        <div
          class="w-7 h-7 rounded-card grid place-items-center"
          style="background-image: linear-gradient(135deg, #f43f5e 0%, #b91c1c 100%);"
        >
          <ShieldCheck class="w-4 h-4 text-white" />
        </div>
        <div>
          <div class="text-xs font-semibold text-slate-100 tracking-tight">
            {{ t('admin.title') }}
          </div>
          <div class="text-[10px] text-slate-400 uppercase tracking-wider">admin</div>
        </div>
      </div>

      <button
        v-for="item in items"
        :key="item.name"
        type="button"
        @click="go(item)"
        :disabled="!item.routeName"
        :title="item.blockedReason"
        class="w-full text-left px-2.5 py-2 rounded-input flex items-center gap-2.5 text-sm transition-colors relative"
        :class="[
          isActive(item)
            ? 'bg-slate-700/70 text-slate-50'
            : item.routeName
              ? 'text-slate-300 hover:bg-slate-800/70 hover:text-slate-100'
              : 'text-slate-600 cursor-not-allowed',
        ]"
      >
        <component :is="item.icon" class="w-4 h-4 shrink-0" />
        <span class="flex-1">{{ t(`admin.nav_${item.name}`) }}</span>
        <span
          v-if="!item.routeName"
          class="text-[9px] uppercase tracking-wider text-slate-600 border border-slate-700 rounded px-1 py-px"
        >
          {{ t('admin.soon') }}
        </span>
      </button>

      <button
        type="button"
        @click="backToApp"
        class="mt-auto px-2.5 py-2 rounded-input flex items-center gap-2.5 text-sm text-slate-400 hover:bg-slate-800/70 hover:text-slate-100 transition-colors"
      >
        <ArrowLeft class="w-4 h-4" />
        {{ t('admin.back_to_app') }}
      </button>
    </nav>

    <div class="flex-1 flex flex-col min-w-0">
      <!-- admin 顶部条(深色识别 admin 路由)-->
      <header
        class="h-12 flex items-center justify-between px-4 lg:px-6 sticky top-0 z-30 border-b border-slate-700/60"
        style="background-color: rgb(15 23 42);"
      >
        <div class="flex items-center gap-2.5">
          <span class="text-sm font-semibold tracking-tight text-slate-100">
            DataOps Studio
          </span>
          <span
            class="text-[10px] font-mono font-semibold px-1.5 py-0.5 rounded-input uppercase tracking-wider"
            style="background-color: rgb(244 63 94 / 0.15); color: rgb(251 113 133);"
          >
            {{ t('admin.badge') }}
          </span>
        </div>
        <UserMenu />
      </header>

      <LicenseBanner />

      <main class="flex-1 overflow-y-auto">
        <RouterView />
      </main>
    </div>
  </div>
</template>
