<script setup lang="ts">
/**
 * TraceCompareDialog —— C-8 逐跳血缘对比入口(两域联动:血缘 x 对比)。
 *
 * 给定焦点字段(table.column)+ 两套环境数据源 + 共享主键,调 trace-compare 端点:
 *  - 预览(dry_run=true):后端沿上游血缘每跳算出 compare 计划,列出每跳对比表 / 追踪列 / 主键;
 *  - 创建(dry_run=false):把计划落成 workflow(只创建不触发),返回 workflow_id。
 *
 * 字段全部锚 api/lineage.ts(锚后端 schemas.py TraceCompareRequest/Response)。
 * ★ workflow 管理前端(W-A)尚未落地,创建后只回执 workflow_id,不导航到 workflow 页。
 */
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { GitCompareArrows } from 'lucide-vue-next'
import Modal from './Modal.vue'
import LoadingDots from './LoadingDots.vue'
import {
  createTraceCompare,
  type TraceCompareResponse,
} from '../api/lineage'
import { ApiError, type DatasourceListItem } from '../api/types'

const props = defineProps<{
  open: boolean
  projectId: string
  focus: string
  datasources: DatasourceListItem[]
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'created', workflowId: string): void
}>()

const { t } = useI18n()

const sourceId = ref('')
const targetId = ref('')
const keyColumnsRaw = ref('')
const maxHops = ref(10)
const name = ref('')

const previewData = ref<TraceCompareResponse | null>(null)
const createdData = ref<TraceCompareResponse | null>(null)
const busy = ref(false)
const mode = ref<'preview' | 'create' | null>(null)
const error = ref<string | null>(null)

// focus 必须精确到列:至少 table.column 两段(schema.table.column 也算,>=2 段且末段非空)
const isColumnFocus = computed(() => {
  const parts = props.focus.split('.')
  return parts.length >= 2 && parts.every((part) => part.length > 0)
})

const keyColumns = computed(() =>
  keyColumnsRaw.value
    .split(',')
    .map((token) => token.trim())
    .filter((token) => token.length > 0),
)

const sameDatasource = computed(
  () => Boolean(sourceId.value) && sourceId.value === targetId.value,
)

const canSubmit = computed(
  () =>
    isColumnFocus.value &&
    Boolean(sourceId.value) &&
    Boolean(targetId.value) &&
    !sameDatasource.value &&
    keyColumns.value.length > 0 &&
    !busy.value,
)

// 每次打开重置(避免残留上次的预览 / 结果)
watch(
  () => props.open,
  (open) => {
    if (open) {
      previewData.value = null
      createdData.value = null
      error.value = null
      mode.value = null
    }
  },
)

async function submit(dryRun: boolean): Promise<void> {
  error.value = null
  if (!isColumnFocus.value) {
    error.value = t('lineage.tc_not_column')
    return
  }
  if (sameDatasource.value) {
    error.value = t('lineage.tc_same_ds')
    return
  }
  if (keyColumns.value.length === 0) {
    error.value = t('lineage.tc_keys_required')
    return
  }
  busy.value = true
  mode.value = dryRun ? 'preview' : 'create'
  try {
    const res = await createTraceCompare(props.projectId, {
      focus: props.focus,
      source_id: sourceId.value,
      target_id: targetId.value,
      key_columns: keyColumns.value,
      max_hops: maxHops.value,
      name: name.value.trim() || null,
      dry_run: dryRun,
    })
    if (dryRun) {
      previewData.value = res
    } else {
      createdData.value = res
      if (res.workflow_id) emit('created', res.workflow_id)
    }
  } catch (e) {
    if (e instanceof ApiError) {
      error.value = e.code ? `${e.code}: ${e.message}` : e.message
    } else {
      error.value = (e as Error).message
    }
  } finally {
    busy.value = false
    mode.value = null
  }
}
</script>

<template>
  <Modal :open="open" :title="t('lineage.tc_title')" :subtitle="t('lineage.tc_subtitle')" @close="emit('close')">
    <div class="space-y-4">
      <!-- 焦点字段 -->
      <div>
        <span class="block text-xs chrome-text-muted mb-1">{{ t('lineage.tc_focus') }}</span>
        <div class="font-mono text-sm chrome-text-heading break-all">{{ focus || '—' }}</div>
        <p v-if="!isColumnFocus" class="text-xs text-amber-500 mt-1">
          {{ t('lineage.tc_not_column') }}
        </p>
        <p v-else class="text-xs chrome-text-muted mt-1">{{ t('lineage.tc_focus_hint') }}</p>
      </div>

      <!-- 两侧数据源 -->
      <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <label class="block">
          <span class="block text-xs chrome-text-muted mb-1">{{ t('lineage.tc_source') }}</span>
          <select v-model="sourceId" class="chrome-input w-full">
            <option value="">{{ t('lineage.tc_pick_ds') }}</option>
            <option v-for="ds in datasources" :key="ds.id" :value="ds.id">{{ ds.name }}</option>
          </select>
        </label>
        <label class="block">
          <span class="block text-xs chrome-text-muted mb-1">{{ t('lineage.tc_target') }}</span>
          <select v-model="targetId" class="chrome-input w-full">
            <option value="">{{ t('lineage.tc_pick_ds') }}</option>
            <option v-for="ds in datasources" :key="ds.id" :value="ds.id">{{ ds.name }}</option>
          </select>
        </label>
      </div>
      <p v-if="sameDatasource" class="text-xs text-amber-500 -mt-2">{{ t('lineage.tc_same_ds') }}</p>

      <!-- 主键 + 跳数 + 名称 -->
      <label class="block">
        <span class="block text-xs chrome-text-muted mb-1">{{ t('lineage.tc_keys') }}</span>
        <input v-model="keyColumnsRaw" type="text" class="chrome-input w-full" :placeholder="t('lineage.tc_keys_ph')" />
      </label>
      <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <label class="block">
          <span class="block text-xs chrome-text-muted mb-1">{{ t('lineage.tc_max_hops') }}</span>
          <input v-model.number="maxHops" type="number" min="1" max="20" class="chrome-input w-full" />
        </label>
        <label class="block">
          <span class="block text-xs chrome-text-muted mb-1">{{ t('lineage.tc_name') }}</span>
          <input v-model="name" type="text" class="chrome-input w-full" :placeholder="t('lineage.tc_name_ph')" />
        </label>
      </div>

      <p v-if="error" class="text-sm text-red-500">{{ error }}</p>

      <!-- 预览计划 -->
      <div v-if="previewData && !createdData" class="border chrome-border-subtle rounded-card overflow-hidden">
        <div class="px-3 py-2 text-xs font-semibold chrome-text-heading border-b chrome-border-subtle">
          {{ t('lineage.tc_preview_title', { count: previewData.hop_count }) }}
        </div>
        <div class="overflow-x-auto">
          <table class="w-full text-xs">
            <thead class="chrome-text-muted">
              <tr class="text-left">
                <th class="px-3 py-1.5 font-medium">{{ t('lineage.tc_hop_col_node') }}</th>
                <th class="px-3 py-1.5 font-medium">{{ t('lineage.tc_hop_col_table') }}</th>
                <th class="px-3 py-1.5 font-medium">{{ t('lineage.tc_hop_col_column') }}</th>
                <th class="px-3 py-1.5 font-medium">{{ t('lineage.tc_hop_col_keys') }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="hop in previewData.hops" :key="hop.node_id" class="border-t chrome-border-subtle">
                <td class="px-3 py-1.5 font-mono chrome-text-muted">{{ hop.node_id }}</td>
                <td class="px-3 py-1.5 font-mono chrome-text-heading">{{ hop.table }}</td>
                <td class="px-3 py-1.5 font-mono">{{ hop.column }}</td>
                <td class="px-3 py-1.5 font-mono chrome-text-muted">{{ hop.key_columns.join(', ') }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <p v-if="previewData.truncated" class="px-3 py-2 text-xs text-amber-500 border-t chrome-border-subtle">
          {{ t('lineage.tc_truncated') }}
        </p>
      </div>

      <!-- 创建成功回执 -->
      <p v-if="createdData?.workflow_id" class="text-sm text-emerald-500">
        {{ t('lineage.tc_created', { name: createdData.workflow_name ?? createdData.workflow_id }) }}
      </p>

      <!-- 动作 -->
      <div v-if="!createdData" class="flex items-center justify-end gap-2 pt-1">
        <button
          type="button"
          class="chrome-btn-secondary text-sm"
          :disabled="!canSubmit"
          @click="submit(true)"
        >
          <LoadingDots v-if="busy && mode === 'preview'" />
          <span v-else>{{ t('lineage.tc_preview') }}</span>
        </button>
        <button
          type="button"
          class="chrome-btn-primary text-sm inline-flex items-center gap-1.5"
          :disabled="!canSubmit"
          @click="submit(false)"
        >
          <LoadingDots v-if="busy && mode === 'create'" />
          <template v-else>
            <GitCompareArrows class="w-4 h-4" />
            {{ t('lineage.tc_create') }}
          </template>
        </button>
      </div>
      <div v-else class="flex items-center justify-end pt-1">
        <button type="button" class="chrome-btn-primary text-sm" @click="emit('close')">OK</button>
      </div>
    </div>
  </Modal>
</template>
