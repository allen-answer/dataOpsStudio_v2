<script setup lang="ts">
import { Search } from 'lucide-vue-next'
import { useI18n } from 'vue-i18n'
import NavRail from '../components/NavRail.vue'
import UserMenu from '../components/UserMenu.vue'
import LicenseBanner from '../components/LicenseBanner.vue'

const { t } = useI18n()
</script>

<template>
  <div class="min-h-screen flex flex-1 chrome-bg-main">
    <NavRail />

    <div class="flex-1 flex flex-col min-w-0">
      <!-- 顶栏:slim,品牌 + 2.0 badge + 搜索 + 用户菜单 -->
      <header
        class="h-12 border-b chrome-border chrome-bg-panel flex items-center justify-between px-4 lg:px-6 sticky top-0 z-30 backdrop-blur-md"
        style="background-color: rgb(var(--bg-panel) / 0.92);"
      >
        <div class="flex items-center gap-2.5">
          <span class="text-section font-semibold tracking-tight chrome-text-heading">
            DataOps Studio
          </span>
          <span
            class="text-[10px] font-mono font-semibold px-1.5 py-0.5 rounded-input chrome-accent-light-bg chrome-accent uppercase tracking-wider"
          >
            2.0
          </span>
        </div>

        <div class="flex items-center gap-2">
          <!-- 搜索占位(★ 后补:全局搜索 / Cmd+K,后端 R7 后做)-->
          <button
            type="button"
            class="hidden md:inline-flex items-center gap-2 px-2.5 py-1 rounded-input chrome-border border chrome-text-muted text-xs hover:chrome-bg-elevated transition-colors"
            :title="t('common.search_placeholder')"
            disabled
          >
            <Search class="w-3.5 h-3.5" />
            <span>{{ t('common.search_placeholder') }}</span>
            <span class="font-mono ml-1 opacity-70">⌘K</span>
          </button>
          <UserMenu />
        </div>
      </header>

      <LicenseBanner />

      <main class="flex-1 overflow-y-auto">
        <RouterView />
      </main>
    </div>
  </div>
</template>
