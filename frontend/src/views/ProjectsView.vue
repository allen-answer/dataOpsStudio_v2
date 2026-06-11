<script setup lang="ts">
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useQuery } from '@tanstack/vue-query'
import { LayoutGrid, Plus, ChevronRight, AlertTriangle } from 'lucide-vue-next'
import { listProjects } from '../api/projects'
import { ApiError, type Project } from '../api/types'
import EmptyState from '../components/EmptyState.vue'
import LoadingDots from '../components/LoadingDots.vue'

const { t } = useI18n()
const router = useRouter()

const query = useQuery({
  queryKey: ['projects'],
  queryFn: listProjects,
})

const projects = computed<Project[]>(() => query.data.value ?? [])

function open(project: Project): void {
  void router.push({ name: 'datasources', params: { id: project.id } })
}

function countMessage(n: number): string {
  if (n === 0) return t('projects.count_zero')
  if (n === 1) return t('projects.count_one')
  return t('projects.count_other', { count: n })
}

function errorMessage(): string {
  const e = query.error.value
  if (e instanceof ApiError) {
    if (e.status === 0) return t('common.error_network')
    return e.message || t('common.error_unknown')
  }
  return t('common.error_unknown')
}
</script>

<template>
  <div class="px-6 lg:px-10 py-10 w-full">
    <!-- Header -->
    <div class="flex items-end justify-between mb-8">
      <div>
        <div class="text-xs uppercase tracking-wider text-slate-500 dark:text-slate-400 font-medium">
          DataOps Studio
        </div>
        <h1 class="text-h2 font-semibold tracking-tight text-slate-800 dark:text-slate-100 mt-1">
          {{ t('projects.title') }}
        </h1>
        <div v-if="!query.isLoading.value && !query.isError.value" class="text-sm text-slate-500 dark:text-slate-400 mt-1">
          {{ countMessage(projects.length) }}
        </div>
      </div>

      <button
        type="button"
        class="btn-secondary opacity-50 cursor-not-allowed"
        :title="t('projects.create_disabled_tip')"
        disabled
      >
        <Plus class="w-4 h-4" />
        {{ t('projects.create') }}
      </button>
    </div>

    <!-- Loading -->
    <div
      v-if="query.isLoading.value"
      class="text-sm text-slate-500 dark:text-slate-400 flex items-center gap-2 py-12 justify-center"
    >
      <LoadingDots />
      <span>{{ t('common.loading') }}</span>
    </div>

    <!-- Error -->
    <div
      v-else-if="query.isError.value"
      class="border border-red-200 dark:border-red-500/30 bg-red-50 dark:bg-red-500/10 rounded-card p-5 flex items-start gap-3"
    >
      <AlertTriangle class="w-5 h-5 text-red-500 dark:text-red-400 shrink-0 mt-0.5" />
      <div>
        <div class="text-sm font-medium text-red-700 dark:text-red-400">{{ t('common.error') }}</div>
        <div class="text-sm text-red-600 dark:text-red-300 mt-0.5">{{ errorMessage() }}</div>
        <button @click="query.refetch()" type="button" class="text-xs text-red-700 dark:text-red-400 underline mt-2">
          {{ t('common.retry') }}
        </button>
      </div>
    </div>

    <!-- Empty -->
    <div
      v-else-if="projects.length === 0"
      class="bg-white dark:bg-slate-800/60 border border-slate-200 dark:border-slate-700 rounded-card shadow-subtle"
    >
      <EmptyState :icon="LayoutGrid" :title="t('projects.empty_title')" :hint="t('projects.empty_hint')" />
    </div>

    <!-- Grid -->
    <div v-else class="grid sm:grid-cols-2 lg:grid-cols-3 gap-4">
      <button
        v-for="p in projects"
        :key="p.id"
        type="button"
        @click="open(p)"
        class="text-left bg-white dark:bg-slate-800/60 border border-slate-200 dark:border-slate-700 rounded-card shadow-subtle p-6 hover:border-sky-300 dark:hover:border-sky-500/40 transition-all group"
      >
        <div class="flex items-start justify-between mb-3">
          <div class="w-10 h-10 rounded-card bg-sky-gradient-soft border grid place-items-center">
            <LayoutGrid class="w-5 h-5 text-sky-500 dark:text-sky-400" />
          </div>
          <ChevronRight
            class="w-4 h-4 text-slate-300 dark:text-slate-600 group-hover:text-sky-500 dark:group-hover:text-sky-400 transition-colors"
          />
        </div>
        <div class="text-section font-semibold tracking-tight text-slate-800 dark:text-slate-100 truncate">
          {{ p.name }}
        </div>
        <div class="text-sm text-slate-500 dark:text-slate-400 mt-1 line-clamp-2 min-h-[2.5rem]">
          {{ p.description || t('projects.no_description') }}
        </div>
        <div class="text-xs text-slate-400 dark:text-slate-500 font-mono mt-3 truncate">
          {{ p.id }}
        </div>
      </button>
    </div>
  </div>
</template>

<style scoped>
.btn-secondary {
  @apply inline-flex items-center justify-center gap-1.5 px-4 py-2 text-ui font-medium select-none whitespace-nowrap transition-all duration-150 bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 text-slate-700 dark:text-slate-200;
  border-radius: var(--radius-card);
}
.btn-secondary:hover:not(:disabled) {
  @apply bg-slate-50 dark:bg-slate-700 border-slate-300 dark:border-slate-600;
}
</style>
