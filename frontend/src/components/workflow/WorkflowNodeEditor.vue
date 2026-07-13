<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { ChevronDown, ChevronUp, Trash2 } from 'lucide-vue-next'

import type { CompareTaskResponse } from '../../api/compare'
import type { DatasourceListItem } from '../../api/types'
import {
  SUPPORTED_WORKFLOW_NODE_KINDS,
  type NotifyTargetInSpec,
  type WorkflowEdge,
  type WorkflowNode,
  type WorkflowNodeKind,
} from '../../api/workflow'
import { cloneValue, defaultPayload } from './workflowEditor'

const props = defineProps<{
  index: number
  nodes: WorkflowNode[]
  edges: WorkflowEdge[]
  datasources: DatasourceListItem[]
  compareTasks: CompareTaskResponse[]
  notifications: NotifyTargetInSpec[]
  createMode: boolean
  first: boolean
  last: boolean
}>()
const emit = defineEmits<{
  remove: []
  up: []
  down: []
  ensureUpstream: [previous: string, source: string, target: string]
  routingChange: []
}>()
const node = defineModel<WorkflowNode>({ required: true })
const { t } = useI18n()

const runnableCompareTasks = computed(() =>
  props.compareTasks.filter(
    (task) => task.source_ref.kind !== 'file' && task.target_ref.kind !== 'file',
  ),
)
const hasFileCompareTasks = computed(() =>
  props.compareTasks.some(
    (task) => task.source_ref.kind === 'file' || task.target_ref.kind === 'file',
  ),
)
const notifyUnavailable = computed(
  () => props.createMode || props.notifications.length === 0,
)
const exportSources = computed(() =>
  props.nodes
    .slice(0, props.index)
    .filter((candidate) =>
      candidate.job_kind === 'sql_query' || candidate.job_kind === 'sql_explain',
    ),
)

function payloadString(key: string): string {
  const value = node.value.payload[key]
  return typeof value === 'string' ? value : ''
}

function payloadNumber(key: string, fallback: number): number {
  const value = node.value.payload[key]
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback
}

function setPayloadString(key: string, event: Event): void {
  node.value.payload[key] = (event.target as HTMLInputElement | HTMLTextAreaElement).value
}

function setPayloadNumber(key: string, event: Event): void {
  node.value.payload[key] = Number((event.target as HTMLInputElement).value)
}

function changeKind(event: Event): void {
  const kind = (event.target as HTMLSelectElement).value as WorkflowNodeKind
  if (notifyUnavailable.value && kind === 'notify') return
  node.value.job_kind = kind
  node.value.payload = defaultPayload(kind)
  emit('routingChange')
}

function changeOnFailure(event: Event): void {
  const value = (event.target as HTMLSelectElement).value
  node.value.on_failure =
    value === 'continue' || value === 'branch' ? value : 'abort'
  emit('routingChange')
}

function setRetry(enabled: boolean): void {
  node.value.retry_policy = enabled ? { max_retries: 0, backoff_seconds: 0 } : null
}

function snapshotCompare(event: Event): void {
  const taskId = (event.target as HTMLSelectElement).value
  const task = runnableCompareTasks.value.find((candidate) => candidate.id === taskId)
  if (!task) {
    node.value.payload = { task_id: '' }
    return
  }
  node.value.payload = {
    task_id: task.id,
    source_id: task.source_id,
    target_id: task.target_id,
    source_ref: cloneValue(task.source_ref),
    target_ref: cloneValue(task.target_ref),
    columns: cloneValue(task.columns),
    compare_rules: cloneValue(task.compare_rules),
    run_limits: cloneValue(task.run_limits),
  }
}

function setExportSource(event: Event): void {
  const previous = selectedExportSource()
  const source = (event.target as HTMLSelectElement).value
  node.value.payload.source_result_set_id = source
    ? '${nodes.' + source + '.result_set_id}'
    : ''
  emit('ensureUpstream', previous, source, node.value.id)
}

function selectedExportSource(): string {
  const value = payloadString('source_result_set_id')
  return props.nodes.find(
    (source) => value === '${nodes.' + source.id + '.result_set_id}',
  )?.id ?? ''
}

function targetIds(): string[] {
  const value = node.value.payload.target_ids
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []
}

function toggleTarget(targetId: string, checked: boolean): void {
  const next = targetIds().filter((id) => id !== targetId)
  if (checked) next.push(targetId)
  node.value.payload.target_ids = next
}
</script>

<template>
  <article
    class="relative rounded-card border chrome-border chrome-bg-panel p-4"
    :data-testid="`workflow-node-${index}`"
  >
    <div
      class="absolute -left-9 top-4 flex h-7 w-7 items-center justify-center rounded-full border border-sky-300 bg-sky-50 font-mono text-[11px] font-semibold text-sky-700 dark:border-sky-500/40 dark:bg-sky-500/10 dark:text-sky-300"
      aria-hidden="true"
    >
      {{ String(index + 1).padStart(2, '0') }}
    </div>

    <div class="mb-3 flex items-center gap-2">
      <span class="text-xs font-semibold chrome-text-heading">{{ t('workflow.editor.node') }}</span>
      <span class="font-mono text-[11px] chrome-text-muted">{{ node.id }}</span>
      <div class="ml-auto flex items-center gap-1">
        <button
          type="button"
          class="chrome-btn-ghost"
          :disabled="first"
          :aria-label="t('workflow.editor.move_up')"
          @click="emit('up')"
        >
          <ChevronUp class="h-4 w-4" />
        </button>
        <button
          type="button"
          class="chrome-btn-ghost"
          :disabled="last"
          :aria-label="t('workflow.editor.move_down')"
          @click="emit('down')"
        >
          <ChevronDown class="h-4 w-4" />
        </button>
        <button
          type="button"
          class="chrome-btn-ghost text-red-600 dark:text-red-400"
          :aria-label="t('workflow.editor.remove_node')"
          @click="emit('remove')"
        >
          <Trash2 class="h-4 w-4" />
        </button>
      </div>
    </div>

    <div class="grid gap-3 md:grid-cols-2">
      <label class="block">
        <span class="mb-1 block text-xs chrome-text-muted">{{ t('workflow.editor.node_id') }}</span>
        <input v-model="node.id" class="chrome-input w-full font-mono text-sm" />
      </label>
      <label class="block">
        <span class="mb-1 block text-xs chrome-text-muted">{{ t('workflow.editor.kind') }}</span>
        <select
          :value="node.job_kind"
          class="chrome-input w-full font-mono text-sm"
          :data-testid="`node-kind-${index}`"
          @change="changeKind"
        >
          <option
            v-for="kind in SUPPORTED_WORKFLOW_NODE_KINDS"
            :key="kind"
            :value="kind"
            :disabled="notifyUnavailable && kind === 'notify'"
          >
            {{ kind }}
          </option>
        </select>
      </label>
      <label class="block">
        <span class="mb-1 block text-xs chrome-text-muted">{{ t('workflow.editor.timeout') }}</span>
        <input
          v-model.number="node.timeout_seconds"
          type="number"
          min="1"
          class="chrome-input w-full text-sm"
        />
      </label>
      <label class="block">
        <span class="mb-1 block text-xs chrome-text-muted">{{ t('workflow.editor.on_failure') }}</span>
        <select
          :value="node.on_failure"
          class="chrome-input w-full text-sm"
          @change="changeOnFailure"
        >
          <option value="abort">abort</option>
          <option value="continue">continue</option>
          <option value="branch">branch</option>
        </select>
      </label>
    </div>

    <label class="mt-3 block">
      <span class="mb-1 block text-xs chrome-text-muted">{{ t('workflow.editor.when') }}</span>
      <input
        :value="node.when ?? ''"
        maxlength="512"
        class="chrome-input w-full font-mono text-sm"
        :placeholder="t('workflow.editor.when_ph')"
        @input="node.when = ($event.target as HTMLInputElement).value || null"
      />
    </label>

    <div class="mt-3 rounded-input border chrome-border-subtle p-3">
      <label class="flex items-center gap-2 text-xs chrome-text-normal">
        <input
          type="checkbox"
          :checked="node.retry_policy !== null"
          @change="setRetry(($event.target as HTMLInputElement).checked)"
        />
        {{ t('workflow.editor.retry') }}
      </label>
      <div v-if="node.retry_policy" class="mt-2 grid gap-3 sm:grid-cols-2">
        <label class="block text-xs chrome-text-muted">
          {{ t('workflow.editor.max_retries') }}
          <input
            v-model.number="node.retry_policy.max_retries"
            type="number"
            min="0"
            max="5"
            class="chrome-input mt-1 w-full text-sm"
          />
        </label>
        <label class="block text-xs chrome-text-muted">
          {{ t('workflow.editor.backoff') }}
          <input
            v-model.number="node.retry_policy.backoff_seconds"
            type="number"
            min="0"
            max="3600"
            class="chrome-input mt-1 w-full text-sm"
          />
        </label>
      </div>
    </div>

    <div class="mt-3 border-t chrome-border-subtle pt-3">
      <template v-if="node.job_kind === 'sql_query' || node.job_kind === 'sql_explain'">
        <div class="grid gap-3 md:grid-cols-2">
          <label class="block">
            <span class="mb-1 block text-xs chrome-text-muted">{{ t('workflow.editor.datasource') }}</span>
            <select
              :value="payloadString('datasource_id')"
              class="chrome-input w-full text-sm"
              @change="setPayloadString('datasource_id', $event)"
            >
              <option value="">{{ t('workflow.editor.choose') }}</option>
              <option v-for="source in datasources" :key="source.id" :value="source.id">
                {{ source.name }}
              </option>
            </select>
          </label>
        </div>
        <label class="mt-3 block">
          <span class="mb-1 block text-xs chrome-text-muted">{{ t('workflow.editor.sql') }}</span>
          <textarea
            :value="payloadString('sql')"
            rows="4"
            spellcheck="false"
            class="chrome-input w-full font-mono text-sm"
            @input="setPayloadString('sql', $event)"
          />
        </label>
      </template>

      <template v-else-if="node.job_kind === 'compare_run'">
        <label class="block">
          <span class="mb-1 block text-xs chrome-text-muted">{{ t('workflow.editor.compare_task') }}</span>
          <select
            :value="payloadString('task_id')"
            class="chrome-input w-full text-sm"
            :data-testid="`compare-task-${index}`"
            @change="snapshotCompare"
          >
            <option value="">{{ t('workflow.editor.choose') }}</option>
            <option v-for="task in runnableCompareTasks" :key="task.id" :value="task.id">
              {{ task.name }}
            </option>
          </select>
        </label>
        <p v-if="hasFileCompareTasks" class="mt-2 text-[11px] text-amber-700 dark:text-amber-300">
          {{ t('workflow.editor.compare_file_unavailable') }}
        </p>
      </template>

      <template v-else-if="node.job_kind === 'lineage_analyze'">
        <div class="grid gap-3 md:grid-cols-2">
          <label class="block">
            <span class="mb-1 block text-xs chrome-text-muted">{{ t('workflow.editor.datasource') }}</span>
            <select
              :value="payloadString('datasource_id')"
              class="chrome-input w-full text-sm"
              @change="setPayloadString('datasource_id', $event)"
            >
              <option value="">{{ t('workflow.editor.choose') }}</option>
              <option v-for="source in datasources" :key="source.id" :value="source.id">
                {{ source.name }}
              </option>
            </select>
          </label>
          <label class="block">
            <span class="mb-1 block text-xs chrome-text-muted">{{ t('workflow.editor.dialect') }}</span>
            <input
              :value="payloadString('dialect')"
              class="chrome-input w-full font-mono text-sm"
              @input="setPayloadString('dialect', $event)"
            />
          </label>
          <label class="block">
            <span class="mb-1 block text-xs chrome-text-muted">{{ t('workflow.editor.default_schema') }}</span>
            <input
              :value="payloadString('default_schema')"
              class="chrome-input w-full font-mono text-sm"
              @input="setPayloadString('default_schema', $event)"
            />
          </label>
          <label class="block">
            <span class="mb-1 block text-xs chrome-text-muted">{{ t('workflow.editor.source_ref') }}</span>
            <input
              :value="payloadString('source_ref')"
              class="chrome-input w-full font-mono text-sm"
              @input="setPayloadString('source_ref', $event)"
            />
          </label>
        </div>
        <label class="mt-3 block">
          <span class="mb-1 block text-xs chrome-text-muted">{{ t('workflow.editor.sql') }}</span>
          <textarea
            :value="payloadString('sql_text')"
            rows="4"
            class="chrome-input w-full font-mono text-sm"
            @input="setPayloadString('sql_text', $event)"
          />
        </label>
      </template>

      <template v-else-if="node.job_kind === 'export_excel'">
        <div class="grid gap-3 md:grid-cols-2">
          <label class="block">
            <span class="mb-1 block text-xs chrome-text-muted">{{ t('workflow.editor.upstream_result') }}</span>
            <select
              :value="selectedExportSource()"
              class="chrome-input w-full font-mono text-sm"
              :data-testid="`export-source-${index}`"
              @change="setExportSource"
            >
              <option value="">{{ t('workflow.editor.choose') }}</option>
              <option v-for="source in exportSources" :key="source.id" :value="source.id">
                {{ source.id }}
              </option>
            </select>
          </label>
          <label class="block">
            <span class="mb-1 block text-xs chrome-text-muted">{{ t('workflow.editor.filename') }}</span>
            <input
              :value="payloadString('filename')"
              class="chrome-input w-full font-mono text-sm"
              @input="setPayloadString('filename', $event)"
            />
          </label>
        </div>
        <p class="mt-2 font-mono text-[11px] chrome-text-muted">
          {{ payloadString('source_result_set_id') || t('workflow.editor.export_edge_hint') }}
        </p>
      </template>

      <template v-else-if="node.job_kind === 'notify'">
        <p v-if="createMode" class="text-xs text-amber-700 dark:text-amber-300">
          {{ t('workflow.editor.notify_create_unavailable') }}
        </p>
        <template v-else>
          <div class="text-xs chrome-text-muted">{{ t('workflow.editor.notify_targets') }}</div>
          <div class="mt-2 grid gap-2 sm:grid-cols-2">
            <label
              v-for="target in notifications"
              :key="target.id"
              class="flex items-center gap-2 rounded-input border chrome-border-subtle px-2 py-1.5 text-xs chrome-text-normal"
            >
              <input
                type="checkbox"
                :checked="targetIds().includes(target.id)"
                :data-testid="`notify-target-${node.id}-${target.id}`"
                @change="toggleTarget(target.id, ($event.target as HTMLInputElement).checked)"
              />
              <span>{{ target.channel }}</span>
              <span class="font-mono chrome-text-muted">{{ target.id }}</span>
            </label>
          </div>
          <p v-if="notifications.length === 0" class="mt-2 text-xs text-amber-700 dark:text-amber-300">
            {{ t('workflow.editor.notify_no_targets') }}
          </p>
          <label class="mt-3 block">
            <span class="mb-1 block text-xs chrome-text-muted">{{ t('workflow.editor.message') }}</span>
            <textarea
              :value="payloadString('message')"
              maxlength="512"
              rows="2"
              class="chrome-input w-full text-sm"
              @input="setPayloadString('message', $event)"
            />
          </label>
        </template>
      </template>

      <template v-else-if="node.job_kind === 'sleep'">
        <label class="block max-w-xs">
          <span class="mb-1 block text-xs chrome-text-muted">{{ t('workflow.editor.duration') }}</span>
          <input
            :value="payloadNumber('duration_seconds', 60)"
            type="number"
            min="1"
            max="86400"
            class="chrome-input w-full text-sm"
            @input="setPayloadNumber('duration_seconds', $event)"
          />
        </label>
      </template>

      <p v-else-if="node.job_kind === 'branch'" class="text-xs chrome-text-muted">
        {{ t('workflow.editor.branch_hint') }}
      </p>
    </div>
  </article>
</template>
