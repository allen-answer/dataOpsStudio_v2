<script setup lang="ts">
import NavRail from '../components/NavRail.vue'
import UserMenu from '../components/UserMenu.vue'
import { Sparkles } from 'lucide-vue-next'
</script>

<template>
  <div class="min-h-screen flex">
    <NavRail />

    <div class="flex-1 flex flex-col min-w-0">
      <!-- 顶栏:slim 一条,放品牌名 + 用户菜单 -->
      <header
        class="h-14 border-b border-slate-200 dark:border-slate-800 bg-white/85 dark:bg-slate-900/85 backdrop-blur-md flex items-center justify-between px-4 lg:px-6 sticky top-0 z-30"
      >
        <div class="flex items-center gap-2">
          <Sparkles class="w-4 h-4 text-sky-500 dark:text-sky-400" />
          <span class="text-section font-semibold tracking-tight text-slate-800 dark:text-slate-100">
            DataOps Studio
          </span>
          <span class="text-xs text-slate-400 dark:text-slate-500 font-mono ml-1">2.0</span>
        </div>
        <UserMenu />
      </header>

      <main class="flex-1 overflow-y-auto">
        <RouterView />
      </main>
    </div>
  </div>
</template>
