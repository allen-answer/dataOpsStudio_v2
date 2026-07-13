<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { ChevronDown, ChevronUp, Plus, Trash2 } from 'lucide-vue-next'

import type { WorkflowEdge, WorkflowNode } from '../../api/workflow'

const props = defineProps<{ nodes: WorkflowNode[] }>()
const edges = defineModel<WorkflowEdge[]>({ required: true })
const { t } = useI18n()

const nodeIds = computed(() => new Set(props.nodes.map((node) => node.id)))

function sourceNode(edge: WorkflowEdge): WorkflowNode | undefined {
  return props.nodes.find((node) => node.id === edge.source)
}

function isRouted(edge: WorkflowEdge): boolean {
  const source = sourceNode(edge)
  return (
    (edge.trigger === 'success' && source?.job_kind === 'branch') ||
    (edge.trigger === 'failure' && source?.on_failure === 'branch')
  )
}

function normalizeRoute(index: number): void {
  const edge = edges.value[index]
  if (!edge || isRouted(edge)) return
  edge.when = null
  edge.is_default = false
}

function addEdge(): void {
  const source = props.nodes[0]?.id ?? ''
  const target = props.nodes[1]?.id ?? ''
  edges.value.push({ source, target, trigger: 'success', when: null, is_default: false })
}

function move(index: number, direction: -1 | 1): void {
  const next = index + direction
  if (next < 0 || next >= edges.value.length) return
  const copy = [...edges.value]
  const current = copy[index]
  const other = copy[next]
  if (!current || !other) return
  copy[index] = other
  copy[next] = current
  edges.value = copy
}

function edgeError(edge: WorkflowEdge, index: number): string | null {
  if (!nodeIds.value.has(edge.source) || !nodeIds.value.has(edge.target)) {
    return t('workflow.editor.edge_unknown')
  }
  if (edge.source === edge.target) return t('workflow.editor.edge_self')
  const duplicate = edges.value.findIndex(
    (candidate) =>
      candidate.source === edge.source &&
      candidate.target === edge.target &&
      candidate.trigger === edge.trigger,
  )
  if (duplicate !== index) return t('workflow.editor.edge_duplicate')
  return null
}

const routingErrors = computed(() => {
  const messages: string[] = []
  const groups = new Map<string, WorkflowEdge[]>()
  for (const edge of edges.value) {
    if (!isRouted(edge)) continue
    const key = `${edge.source}:${edge.trigger}`
    groups.set(key, [...(groups.get(key) ?? []), edge])
  }
  for (const node of props.nodes) {
    if (node.job_kind === 'branch') {
      const key = `${node.id}:success`
      if (!groups.has(key)) groups.set(key, [])
    }
    if (node.on_failure === 'branch') {
      const key = `${node.id}:failure`
      if (!groups.has(key)) groups.set(key, [])
    }
  }
  for (const [key, routes] of groups) {
    const failureRoutes = key.endsWith(':failure')
    if (routes.length < (failureRoutes ? 1 : 2)) {
      messages.push(
        t(
          failureRoutes
            ? 'workflow.editor.failure_route_minimum'
            : 'workflow.editor.route_minimum',
          { key },
        ),
      )
    }
    if (routes.filter((edge) => edge.is_default).length !== 1) {
      messages.push(t('workflow.editor.route_default', { key }))
    }
    if (
      routes.some((edge) =>
        edge.is_default ? edge.when !== null : !edge.when?.trim(),
      )
    ) {
      messages.push(t('workflow.editor.route_condition', { key }))
    }
  }
  return messages
})
</script>

<template>
  <section class="rounded-card border chrome-border chrome-bg-panel p-4">
    <div class="flex items-center justify-between gap-3">
      <div>
        <h3 class="text-sm font-semibold chrome-text-heading">{{ t('workflow.editor.routes') }}</h3>
        <p class="mt-0.5 text-[11px] chrome-text-muted">
          {{ t('workflow.editor.routes_hint') }}
        </p>
      </div>
      <button type="button" class="chrome-btn-secondary text-xs" @click="addEdge">
        <Plus class="h-3.5 w-3.5" /> {{ t('workflow.editor.add_edge') }}
      </button>
    </div>

    <div class="mt-3 space-y-2">
      <div
        v-for="(edge, index) in edges"
        :key="index"
        class="rounded-input border chrome-border-subtle p-3"
        :data-testid="`edge-row-${index}`"
      >
        <div class="grid gap-2 lg:grid-cols-[1fr_auto_1fr_auto]">
          <label class="block">
            <span class="mb-1 block text-[11px] chrome-text-muted">{{ t('workflow.editor.source') }}</span>
            <select
              v-model="edge.source"
              class="chrome-input w-full font-mono text-xs"
              @change="normalizeRoute(index)"
            >
              <option v-for="node in nodes" :key="node.id" :value="node.id">{{ node.id }}</option>
            </select>
          </label>
          <label class="block">
            <span class="mb-1 block text-[11px] chrome-text-muted">{{ t('workflow.editor.trigger') }}</span>
            <select
              v-model="edge.trigger"
              class="chrome-input w-full text-xs"
              @change="normalizeRoute(index)"
            >
              <option value="success">success</option>
              <option value="failure">failure</option>
            </select>
          </label>
          <label class="block">
            <span class="mb-1 block text-[11px] chrome-text-muted">{{ t('workflow.editor.target') }}</span>
            <select v-model="edge.target" class="chrome-input w-full font-mono text-xs">
              <option v-for="node in nodes" :key="node.id" :value="node.id">{{ node.id }}</option>
            </select>
          </label>
          <div class="flex items-end gap-1">
            <button
              type="button"
              class="chrome-btn-ghost"
              :disabled="index === 0"
              :data-testid="`edge-up-${index}`"
              :aria-label="t('workflow.editor.move_edge_up')"
              @click="move(index, -1)"
            >
              <ChevronUp class="h-4 w-4" />
            </button>
            <button
              type="button"
              class="chrome-btn-ghost"
              :disabled="index === edges.length - 1"
              :data-testid="`edge-down-${index}`"
              :aria-label="t('workflow.editor.move_edge_down')"
              @click="move(index, 1)"
            >
              <ChevronDown class="h-4 w-4" />
            </button>
            <button
              type="button"
              class="chrome-btn-ghost text-red-600 dark:text-red-400"
              :aria-label="t('workflow.editor.remove_edge')"
              @click="edges.splice(index, 1)"
            >
              <Trash2 class="h-4 w-4" />
            </button>
          </div>
        </div>

        <div v-if="isRouted(edge)" class="mt-2 flex flex-wrap items-end gap-3">
          <label class="min-w-64 flex-1">
            <span class="mb-1 block text-[11px] chrome-text-muted">{{ t('workflow.editor.edge_when') }}</span>
            <input
              v-model="edge.when"
              :disabled="edge.is_default"
              maxlength="512"
              class="chrome-input w-full font-mono text-xs"
              :placeholder="t('workflow.editor.edge_when_ph')"
            />
          </label>
          <label class="flex items-center gap-2 pb-2 text-xs chrome-text-normal">
            <input
              v-model="edge.is_default"
              type="checkbox"
              @change="edge.when = edge.is_default ? null : edge.when"
            />
            {{ t('workflow.editor.default_route') }}
          </label>
          <span class="mb-1 rounded-full bg-sky-50 px-2 py-1 font-mono text-[10px] text-sky-700 dark:bg-sky-500/10 dark:text-sky-300">
            {{ t('workflow.editor.first_match') }}
          </span>
        </div>
        <p v-if="edgeError(edge, index)" class="mt-2 text-xs text-red-600 dark:text-red-400">
          {{ edgeError(edge, index) }}
        </p>
      </div>
    </div>

    <p v-if="edges.length === 0" class="mt-3 text-xs chrome-text-muted">
      {{ t('workflow.no_edges') }}
    </p>
    <div v-if="routingErrors.length" class="mt-3 space-y-1 text-xs text-red-600 dark:text-red-400">
      <p v-for="message in routingErrors" :key="message">{{ message }}</p>
    </div>
  </section>
</template>
