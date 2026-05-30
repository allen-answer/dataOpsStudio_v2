<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { LayoutGrid, Database, Terminal, ListChecks, Sparkles } from 'lucide-vue-next'

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
    class="w-16 shrink-0 border-r border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 py-3 flex flex-col items-center gap-1"
  >
    <RouterLink
      :to="{ name: 'projects' }"
      class="w-9 h-9 mb-3 rounded-card bg-sky-gradient grid place-items-center shadow-subtle hover:opacity-90 transition-opacity"
    >
      <Sparkles class="w-4 h-4 text-white" />
    </RouterLink>

    <button
      v-for="item in items"
      :key="item.name"
      type="button"
      @click="go(item)"
      class="w-12 py-2 rounded-card flex flex-col items-center gap-0.5 transition-colors relative"
      :class="
        isActive(item)
          ? 'bg-sky-50 dark:bg-sky-500/15 text-sky-600 dark:text-sky-400'
          : 'text-slate-500 dark:text-slate-400 hover:bg-slate-50 dark:hover:bg-slate-800/60 hover:text-slate-700 dark:hover:text-slate-200'
      "
    >
      <span
        v-if="isActive(item)"
        class="absolute left-0 top-1.5 bottom-1.5 w-0.5 rounded-r bg-sky-500 dark:bg-sky-400"
      ></span>
      <component :is="item.icon" class="w-4 h-4" />
      <span class="text-[10px] font-medium">{{ t(`nav.${item.name}`) }}</span>
    </button>
  </nav>
</template>
