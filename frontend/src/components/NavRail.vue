<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { LayoutGrid, Database, Terminal, ListChecks, Bot } from 'lucide-vue-next'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()

interface NavItem {
  name: string
  icon: typeof LayoutGrid
  to: { name: string; params?: Record<string, string> }
}

const currentProjectId = computed(() => {
  const id = route.params.id
  return typeof id === 'string' ? id : null
})

const items = computed<NavItem[]>(() => {
  const projectId = currentProjectId.value
  const base: NavItem[] = [{ name: 'projects', icon: LayoutGrid, to: { name: 'projects' } }]
  if (projectId) {
    base.push(
      { name: 'datasources', icon: Database, to: { name: 'datasources', params: { id: projectId } } },
      { name: 'sql', icon: Terminal, to: { name: 'sql', params: { id: projectId } } },
      { name: 'jobs', icon: ListChecks, to: { name: 'jobs', params: { id: projectId } } },
    )
  }
  return base
})

function isActive(item: NavItem): boolean {
  return route.name === item.to.name
}
function go(item: NavItem): void {
  void router.push(item.to)
}
</script>

<template>
  <nav
    class="w-16 shrink-0 border-r chrome-border chrome-bg-panel py-3 flex flex-col items-center gap-1"
  >
    <!-- 品牌 D 字母,渐变跟当前 variant 走 -->
    <RouterLink
      :to="{ name: 'projects' }"
      class="w-9 h-9 mb-3 rounded-card grid place-items-center shadow-card hover:opacity-90 transition-opacity"
      style="background-image: var(--gradient-brand);"
      :title="t('common.app_name')"
    >
      <span class="text-white text-base font-bold tracking-tight">D</span>
    </RouterLink>

    <button
      v-for="item in items"
      :key="item.name"
      type="button"
      @click="go(item)"
      class="w-12 py-2 rounded-card flex flex-col items-center gap-0.5 transition-colors relative chrome-nav-item"
      :class="isActive(item) && 'chrome-nav-item-active'"
    >
      <!-- 选中状态左侧立柱 -->
      <span
        v-if="isActive(item)"
        class="absolute left-0 top-1.5 bottom-1.5 w-0.5 rounded-r chrome-accent-bg"
      />
      <component :is="item.icon" class="w-4 h-4" />
      <span class="text-[10px] font-medium">{{ t(`nav.${item.name}`) }}</span>
    </button>

    <!-- 底部 AI Copilot 入口(★ 后补:T7+1 接 AI Service)-->
    <button
      type="button"
      class="mt-auto w-9 h-9 mb-1 rounded-card grid place-items-center transition-colors chrome-btn-ai"
      :title="t('nav.ai_copilot')"
    >
      <Bot class="w-4 h-4" />
    </button>
  </nav>
</template>

<style scoped>
.chrome-nav-item {
  color: rgb(var(--text-muted));
}
.chrome-nav-item:hover {
  background-color: rgb(var(--bg-panel-elevated));
  color: rgb(var(--text-heading));
}
.chrome-nav-item-active,
.chrome-nav-item-active:hover {
  background-color: rgb(var(--accent) / 0.12);
  color: rgb(var(--accent));
}

.chrome-btn-ai {
  color: rgb(var(--accent));
  background-color: rgb(var(--accent) / 0.10);
  border: 1px solid rgb(var(--accent) / 0.30);
}
.chrome-btn-ai:hover {
  background-color: rgb(var(--accent) / 0.18);
}
</style>
