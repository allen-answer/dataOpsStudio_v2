<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { Braces, Plus, Save, X } from 'lucide-vue-next'

import type { CompareTaskResponse } from '../../api/compare'
import type { DatasourceListItem } from '../../api/types'
import type { NotifyTargetInSpec, WorkflowEdge, WorkflowNode, WorkflowSpec } from '../../api/workflow'
import WorkflowEdgeEditor from './WorkflowEdgeEditor.vue'
import WorkflowNodeEditor from './WorkflowNodeEditor.vue'
import {
  cloneValue,
  defaultNode,
  emptyWorkflowSpec,
  ensureSuccessEdge,
  normalizeWorkflowSpec,
  specForAdvancedJson,
  variableRows,
  variablesFromRows,
  type VariableRow,
  type WorkflowEditorValue,
} from './workflowEditor'

const props = defineProps<{
  initialName?: string
  initialEnabled?: boolean
  initialSpec?: WorkflowSpec
  createMode: boolean
  datasources: DatasourceListItem[]
  compareTasks: CompareTaskResponse[]
  notifications: NotifyTargetInSpec[]
  busy?: boolean
}>()
const emit = defineEmits<{
  save: [value: WorkflowEditorValue]
  cancel: []
}>()
const { t } = useI18n()

const initial = props.initialSpec ?? emptyWorkflowSpec()
const draft = reactive<WorkflowEditorValue>({
  name: props.initialName ?? '',
  enabled: props.initialEnabled ?? true,
  spec: normalizeWorkflowSpec(initial, props.notifications),
})
const rows = ref<VariableRow[]>(variableRows(draft.spec.variables))
const mode = ref<'form' | 'json'>('form')
const advancedJson = ref('')
const error = ref<string | null>(null)

const VAR_NAME_RE = /^[A-Za-z_][A-Za-z0-9_]*$/
const VAR_VALUE_RE = /^[A-Za-z0-9_.:-]*$/
const BUILTIN_NAMES = new Set(['today', 'now', 'year', 'month', 'day'])

function openMode(next: 'form' | 'json'): void {
  error.value = null
  if (next === mode.value) return
  if (next === 'json') {
    draft.spec.variables = variablesFromRows(rows.value)
    advancedJson.value = JSON.stringify(specForAdvancedJson(draft.spec), null, 2)
    mode.value = 'json'
    return
  }
  const parsed = parseAdvanced()
  if (!parsed) return
  draft.spec = parsed
  rows.value = variableRows(draft.spec.variables)
  mode.value = 'form'
}

function parseAdvanced(): WorkflowSpec | null {
  try {
    const parsed = JSON.parse(advancedJson.value) as unknown
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      const dagOnly = cloneValue(parsed as Record<string, unknown>)
      delete dagOnly.notifications
      return normalizeWorkflowSpec(dagOnly, draft.spec.notifications)
    }
    return normalizeWorkflowSpec(parsed, draft.spec.notifications)
  } catch {
    error.value = t('workflow.editor.invalid_json')
    return null
  }
}

function toggleSchedule(enabled: boolean): void {
  draft.spec.schedule = enabled ? { cron: '0 3 * * *', enabled: true } : null
}

function toggleSensor(enabled: boolean): void {
  draft.spec.sensor = enabled
    ? {
        sql: '',
        datasource_id: props.datasources[0]?.id ?? '',
        check_interval_seconds: 60,
        cooldown_seconds: 300,
        enabled: true,
      }
    : null
}

function addVariable(): void {
  rows.value.push({ name: '', mode: 'scalar', value: '', values: [] })
}

function changeVariableMode(row: VariableRow, modeValue: string): void {
  row.mode = modeValue === 'list' ? 'list' : 'scalar'
  if (row.mode === 'list' && row.values.length === 0) row.values.push('')
}

function addListValue(row: VariableRow): void {
  row.values.push('')
}

function moveNode(index: number, direction: -1 | 1): void {
  const next = index + direction
  if (next < 0 || next >= draft.spec.nodes.length) return
  const nodes = [...draft.spec.nodes]
  const current = nodes[index]
  const other = nodes[next]
  if (!current || !other) return
  nodes[index] = other
  nodes[next] = current
  draft.spec.nodes = nodes
}

function removeNode(index: number): void {
  const removed = draft.spec.nodes[index]
  if (!removed || draft.spec.nodes.length === 1) return
  draft.spec.nodes.splice(index, 1)
  draft.spec.edges = draft.spec.edges.filter(
    (edge) => edge.source !== removed.id && edge.target !== removed.id,
  )
}

function ensureUpstream(previous: string, source: string, target: string): void {
  draft.spec.edges = draft.spec.edges.filter(
    (edge) =>
      !(
        previous &&
        previous !== source &&
        edge.target === target &&
        edge.trigger === 'success' &&
        edge.source === previous
      ),
  )
  if (source) draft.spec.edges = ensureSuccessEdge(draft.spec.edges, source, target)
}

function normalizeNodeRoutes(nodeId: string): void {
  const node = draft.spec.nodes.find((candidate) => candidate.id === nodeId)
  if (!node) return
  draft.spec.edges = draft.spec.edges
    .filter(
      (edge) =>
        !(
          edge.source === nodeId &&
          edge.trigger === 'failure' &&
          node.on_failure !== 'branch'
        ),
    )
    .map((edge) => {
      if (
        edge.source === nodeId &&
        edge.trigger === 'success' &&
        node.job_kind !== 'branch'
      ) {
        return { ...edge, when: null, is_default: false }
      }
      return edge
    })
}

function validateVariableRows(): string | null {
  const names = new Set<string>()
  for (const row of rows.value) {
    const name = row.name.trim()
    if (!name || !VAR_NAME_RE.test(name)) return t('workflow.editor.variable_name_error')
    if (BUILTIN_NAMES.has(name)) return t('workflow.editor.variable_builtin_error', { name })
    if (names.has(name)) return t('workflow.editor.variable_duplicate', { name })
    names.add(name)
    const values = row.mode === 'list' ? row.values : [row.value]
    if (
      values.some(
        (value) => !VAR_VALUE_RE.test(value) || value.includes('--') || value.length > 512,
      )
    ) {
      return t('workflow.editor.variable_value_error', { name })
    }
  }
  return null
}

function routed(edge: WorkflowEdge, nodes: WorkflowNode[]): boolean {
  const source = nodes.find((node) => node.id === edge.source)
  return (
    (edge.trigger === 'success' && source?.job_kind === 'branch') ||
    (edge.trigger === 'failure' && source?.on_failure === 'branch')
  )
}

function validateSpec(spec: WorkflowSpec): string | null {
  if (!draft.name.trim()) return t('workflow.err.name_required')
  const ids = spec.nodes.map((node) => node.id)
  if (ids.some((id) => !id.trim())) return t('workflow.err.invalid_node_id')
  if (new Set(ids).size !== ids.length) return t('workflow.err.duplicate_node_id')
  const idSet = new Set(ids)
  const edgeKeys = new Set<string>()
  for (const edge of spec.edges) {
    if (!idSet.has(edge.source) || !idSet.has(edge.target)) return t('workflow.err.unknown_edge_node')
    if (edge.source === edge.target) return t('workflow.err.self_loop')
    const key = `${edge.source}:${edge.target}:${edge.trigger}`
    if (edgeKeys.has(key)) return t('workflow.editor.edge_duplicate')
    edgeKeys.add(key)
    if (!routed(edge, spec.nodes) && (edge.when || edge.is_default)) {
      return t('workflow.editor.ordinary_edge_rule')
    }
  }
  const routeGroups = new Map<string, WorkflowEdge[]>()
  for (const edge of spec.edges) {
    if (!routed(edge, spec.nodes)) continue
    const key = `${edge.source}:${edge.trigger}`
    routeGroups.set(key, [...(routeGroups.get(key) ?? []), edge])
  }
  for (const routes of routeGroups.values()) {
    const failureRoutes = routes[0]?.trigger === 'failure'
    if (routes.length < (failureRoutes ? 1 : 2)) {
      return t(
        failureRoutes
          ? 'workflow.editor.failure_route_minimum_short'
          : 'workflow.editor.route_minimum_short',
      )
    }
    if (routes.filter((edge) => edge.is_default).length !== 1) {
      return t('workflow.editor.route_default_short')
    }
    if (
      routes.some((edge) =>
        edge.is_default ? edge.when !== null : !edge.when?.trim(),
      )
    ) {
      return t('workflow.editor.route_condition_short')
    }
  }
  for (const node of spec.nodes) {
    if (node.job_kind === 'branch') {
      const routes = spec.edges.filter(
        (edge) => edge.source === node.id && edge.trigger === 'success',
      )
      if (routes.length < 2) return t('workflow.editor.route_minimum_short')
      if (routes.filter((edge) => edge.is_default).length !== 1) {
        return t('workflow.editor.route_default_short')
      }
    }
    if (node.on_failure === 'branch') {
      const routes = spec.edges.filter(
        (edge) => edge.source === node.id && edge.trigger === 'failure',
      )
      if (routes.length < 1) return t('workflow.editor.failure_route_minimum_short')
      if (routes.filter((edge) => edge.is_default).length !== 1) {
        return t('workflow.editor.route_default_short')
      }
    }
    if (node.timeout_seconds < 1) return t('workflow.editor.timeout_error')
    if (node.when && node.when.length > 512) return t('workflow.err.invalid_when')
    if (node.job_kind === 'branch') node.payload = {}
    if (node.job_kind === 'notify') {
      const targets = Array.isArray(node.payload.target_ids) ? node.payload.target_ids : []
      if (targets.length < 1 || targets.length > 10) return t('workflow.editor.notify_target_error')
    }
    if (node.job_kind === 'export_excel') {
      const resultRef =
        typeof node.payload.source_result_set_id === 'string'
          ? node.payload.source_result_set_id
          : ''
      const sourceNode = spec.nodes.find(
        (candidate) =>
          (candidate.job_kind === 'sql_query' || candidate.job_kind === 'sql_explain') &&
          resultRef === '${nodes.' + candidate.id + '.result_set_id}',
      )
      const hasSourceEdge = sourceNode
        ? spec.edges.some(
            (edge) =>
              edge.source === sourceNode.id &&
              edge.target === node.id &&
              edge.trigger === 'success',
          )
        : false
      if (
        !sourceNode ||
        !hasSourceEdge
      ) {
        return t('workflow.editor.export_source_error')
      }
      const filename =
        typeof node.payload.filename === 'string' && node.payload.filename
          ? node.payload.filename
          : 'result.xlsx'
      node.payload.filename = filename.endsWith('.xlsx') ? filename : `${filename}.xlsx`
    }
  }
  return validateVariableRows()
}

function submit(): void {
  error.value = null
  if (mode.value === 'json') {
    const parsed = parseAdvanced()
    if (!parsed) return
    draft.spec = parsed
    rows.value = variableRows(draft.spec.variables)
  } else {
    draft.spec.variables = variablesFromRows(rows.value)
  }
  const validationError = validateSpec(draft.spec)
  if (validationError) {
    error.value = validationError
    return
  }
  emit('save', cloneValue({ ...draft, name: draft.name.trim() }))
}
</script>

<template>
  <div data-testid="workflow-editor" class="mx-auto w-full max-w-5xl pb-8">
    <div class="sticky top-0 z-10 -mx-4 mb-4 border-b chrome-border chrome-bg-main px-4 py-3">
      <div class="flex flex-wrap items-center gap-3">
        <div>
          <div class="text-[11px] font-semibold uppercase tracking-[0.16em] text-sky-600 dark:text-sky-300">
            {{ createMode ? t('workflow.editor.create_eyebrow') : t('workflow.editor.edit_eyebrow') }}
          </div>
          <h2 class="text-base font-semibold chrome-text-heading">
            {{ t('workflow.editor.title') }}
          </h2>
        </div>
        <div class="ml-auto flex items-center rounded-input border chrome-border p-0.5">
          <button
            type="button"
            class="rounded px-3 py-1.5 text-xs transition-colors"
            :class="mode === 'form' ? 'chrome-accent-light-bg chrome-accent' : 'chrome-text-muted'"
            @click="openMode('form')"
          >
            {{ t('workflow.editor.form') }}
          </button>
          <button
            type="button"
            class="rounded px-3 py-1.5 text-xs transition-colors"
            :class="mode === 'json' ? 'chrome-accent-light-bg chrome-accent' : 'chrome-text-muted'"
            @click="openMode('json')"
          >
            <Braces class="mr-1 inline h-3.5 w-3.5" />
            {{ t('workflow.editor.advanced_json') }}
          </button>
        </div>
      </div>
    </div>

    <div v-if="mode === 'form'" class="space-y-4">
      <section class="rounded-card border chrome-border chrome-bg-panel p-4">
        <div class="grid gap-3 md:grid-cols-[1fr_auto]">
          <label class="block">
            <span class="mb-1 block text-xs chrome-text-muted">{{ t('workflow.field_name') }}</span>
            <input
              v-model="draft.name"
              data-testid="workflow-name"
              class="chrome-input w-full text-sm"
              :placeholder="t('workflow.name_ph')"
            />
          </label>
          <label class="flex items-end gap-2 pb-2 text-sm chrome-text-normal">
            <input v-model="draft.enabled" data-testid="workflow-enabled" type="checkbox" />
            {{ t('workflow.editor.workflow_enabled') }}
          </label>
        </div>
      </section>

      <section class="grid gap-4 lg:grid-cols-3">
        <div class="rounded-card border chrome-border chrome-bg-panel p-4">
          <label class="flex items-center gap-2 text-sm font-medium chrome-text-heading">
            <input
              type="checkbox"
              :checked="draft.spec.schedule !== null"
              @change="toggleSchedule(($event.target as HTMLInputElement).checked)"
            />
            {{ t('workflow.section_schedule') }}
          </label>
          <template v-if="draft.spec.schedule">
            <input
              v-model="draft.spec.schedule.cron"
              class="chrome-input mt-3 w-full font-mono text-sm"
              placeholder="0 3 * * *"
            />
            <label class="mt-2 flex items-center gap-2 text-xs chrome-text-normal">
              <input v-model="draft.spec.schedule.enabled" type="checkbox" />
              {{ t('workflow.schedule_on') }}
            </label>
          </template>
        </div>

        <div class="rounded-card border chrome-border chrome-bg-panel p-4 lg:col-span-2">
          <label class="flex items-center gap-2 text-sm font-medium chrome-text-heading">
            <input
              type="checkbox"
              :checked="draft.spec.sensor !== null"
              @change="toggleSensor(($event.target as HTMLInputElement).checked)"
            />
            {{ t('workflow.editor.sensor') }}
          </label>
          <div v-if="draft.spec.sensor" class="mt-3 grid gap-3 md:grid-cols-2">
            <label class="block">
              <span class="mb-1 block text-xs chrome-text-muted">{{ t('workflow.editor.datasource') }}</span>
              <select v-model="draft.spec.sensor.datasource_id" class="chrome-input w-full text-sm">
                <option v-for="source in datasources" :key="source.id" :value="source.id">
                  {{ source.name }}
                </option>
              </select>
            </label>
            <label class="block">
              <span class="mb-1 block text-xs chrome-text-muted">{{ t('workflow.editor.sensor_interval') }}</span>
              <input
                v-model.number="draft.spec.sensor.check_interval_seconds"
                data-testid="sensor-interval"
                type="number"
                min="10"
                max="86400"
                class="chrome-input w-full text-sm"
              />
            </label>
            <label class="block">
              <span class="mb-1 block text-xs chrome-text-muted">{{ t('workflow.editor.sensor_cooldown') }}</span>
              <input
                v-model.number="draft.spec.sensor.cooldown_seconds"
                type="number"
                min="0"
                max="604800"
                class="chrome-input w-full text-sm"
              />
            </label>
            <label class="flex items-end gap-2 pb-2 text-xs chrome-text-normal">
              <input v-model="draft.spec.sensor.enabled" type="checkbox" />
              {{ t('workflow.editor.sensor_enabled') }}
            </label>
            <label class="block md:col-span-2">
              <span class="mb-1 block text-xs chrome-text-muted">{{ t('workflow.editor.sensor_sql') }}</span>
              <textarea
                v-model="draft.spec.sensor.sql"
                rows="3"
                class="chrome-input w-full font-mono text-sm"
              />
            </label>
          </div>
        </div>
      </section>

      <section class="rounded-card border chrome-border chrome-bg-panel p-4">
        <div class="flex items-center justify-between gap-3">
          <div>
            <h3 class="text-sm font-semibold chrome-text-heading">{{ t('workflow.section_variables') }}</h3>
            <p class="text-[11px] chrome-text-muted">{{ t('workflow.editor.variables_hint') }}</p>
          </div>
          <button type="button" class="chrome-btn-secondary text-xs" @click="addVariable">
            <Plus class="h-3.5 w-3.5" /> {{ t('workflow.add_variable') }}
          </button>
        </div>
        <div class="mt-3 space-y-2">
          <div
            v-for="(row, index) in rows"
            :key="index"
            data-testid="variable-row"
            class="rounded-input border chrome-border-subtle p-2"
          >
            <div class="flex flex-wrap items-center gap-2">
              <input
                v-model="row.name"
                class="chrome-input min-w-40 flex-1 font-mono text-sm"
                :placeholder="t('workflow.var_name_ph')"
              />
              <select
                :value="row.mode"
                class="chrome-input text-sm"
                @change="changeVariableMode(row, ($event.target as HTMLSelectElement).value)"
              >
                <option value="scalar">{{ t('workflow.editor.scalar') }}</option>
                <option value="list">{{ t('workflow.editor.list') }}</option>
              </select>
              <input
                v-if="row.mode === 'scalar'"
                v-model="row.value"
                class="chrome-input min-w-40 flex-1 font-mono text-sm"
                :placeholder="t('workflow.var_value_ph')"
              />
              <button
                type="button"
                class="chrome-btn-ghost"
                :aria-label="t('workflow.remove_variable')"
                @click="rows.splice(index, 1)"
              >
                <X class="h-4 w-4" />
              </button>
            </div>
            <div v-if="row.mode === 'list'" class="mt-2 space-y-2 pl-2">
              <div v-for="(_, valueIndex) in row.values" :key="valueIndex" class="flex gap-2">
                <input
                  v-model="row.values[valueIndex]"
                  data-testid="variable-list-value"
                  class="chrome-input flex-1 font-mono text-sm"
                />
                <button
                  type="button"
                  class="chrome-btn-ghost"
                  :aria-label="t('workflow.editor.remove_list_value')"
                  @click="row.values.splice(valueIndex, 1)"
                >
                  <X class="h-4 w-4" />
                </button>
              </div>
              <button type="button" class="chrome-btn-secondary text-xs" @click="addListValue(row)">
                <Plus class="h-3.5 w-3.5" /> {{ t('workflow.editor.add_list_value') }}
              </button>
            </div>
          </div>
        </div>
      </section>

      <section>
        <div class="mb-3 flex items-center justify-between">
          <div>
            <h3 class="text-sm font-semibold chrome-text-heading">
              {{ t('workflow.editor.execution_rail') }}
            </h3>
            <p class="text-[11px] chrome-text-muted">{{ t('workflow.editor.execution_hint') }}</p>
          </div>
          <button
            type="button"
            data-testid="workflow-add-node"
            class="chrome-btn-primary text-xs"
            @click="draft.spec.nodes.push(defaultNode(draft.spec.nodes.length))"
          >
            <Plus class="h-3.5 w-3.5" /> {{ t('workflow.editor.add_node') }}
          </button>
        </div>
        <p
          v-if="createMode"
          class="mb-3 rounded-card border border-amber-300 bg-amber-50 p-2 text-xs text-amber-800 dark:border-amber-500/40 dark:bg-amber-500/10 dark:text-amber-200"
        >
          {{ t('workflow.editor.notify_create_unavailable') }}
        </p>
        <div class="ml-9 space-y-3 border-l border-sky-200 pl-5 dark:border-sky-500/30">
          <WorkflowNodeEditor
            v-for="(node, index) in draft.spec.nodes"
            :key="index"
            v-model="draft.spec.nodes[index]"
            :index="index"
            :nodes="draft.spec.nodes"
            :edges="draft.spec.edges"
            :datasources="datasources"
            :compare-tasks="compareTasks"
            :notifications="draft.spec.notifications"
            :create-mode="createMode"
            :first="index === 0"
            :last="index === draft.spec.nodes.length - 1"
            @remove="removeNode(index)"
            @up="moveNode(index, -1)"
            @down="moveNode(index, 1)"
            @ensure-upstream="ensureUpstream"
            @routing-change="normalizeNodeRoutes(node.id)"
          />
        </div>
      </section>

      <WorkflowEdgeEditor v-model="draft.spec.edges" :nodes="draft.spec.nodes" />
    </div>

    <section v-else class="rounded-card border chrome-border chrome-bg-panel p-4">
      <div class="mb-3 rounded-input border chrome-border-subtle p-2 text-xs chrome-text-muted">
        {{ t('workflow.editor.json_hint') }}
      </div>
      <textarea
        v-model="advancedJson"
        data-testid="advanced-json"
        rows="28"
        spellcheck="false"
        class="chrome-input w-full font-mono text-xs"
      />
    </section>

    <div v-if="error" class="mt-3 text-xs text-red-600 dark:text-red-400">{{ error }}</div>
    <div class="mt-4 flex justify-end gap-2">
      <button type="button" class="chrome-btn-secondary text-sm" @click="emit('cancel')">
        {{ t('common.cancel') }}
      </button>
      <button type="button" class="chrome-btn-primary text-sm" :disabled="busy" @click="submit">
        <Save class="h-4 w-4" /> {{ t('workflow.editor.save') }}
      </button>
    </div>
  </div>
</template>
