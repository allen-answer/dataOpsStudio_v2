<script setup lang="ts">
import { computed, ref } from 'vue'
import { Copy, Info } from 'lucide-vue-next'
import { useI18n } from 'vue-i18n'
import type { CompareProjectionDetail } from '../api/compare'
import Modal from './Modal.vue'

const props = defineProps<{
  name: string
  detail?: CompareProjectionDetail | null
}>()

const { t } = useI18n()
const open = ref(false)
const copied = ref(false)
const copyFailed = ref(false)
const expression = computed(() => props.detail?.expression ?? '')
const summary = computed(() => {
  const oneLine = expression.value.replace(/\s+/g, ' ').trim()
  return oneLine.length > 160 ? `${oneLine.slice(0, 157)}…` : oneLine
})

function close(): void {
  open.value = false
  copied.value = false
  copyFailed.value = false
}

async function copyExpression(): Promise<void> {
  copied.value = false
  copyFailed.value = false
  try {
    await navigator.clipboard.writeText(expression.value)
    copied.value = true
  } catch {
    copyFailed.value = true
  }
}
</script>

<template>
  <span v-if="detail?.generated !== true">{{ name }}</span>
  <template v-else>
    <button
      type="button"
      class="inline-flex items-center gap-1 font-mono chrome-text-heading hover:chrome-accent hover:underline"
      :aria-label="t('compare.expression_inspect', { name })"
      :title="summary"
      @click="open = true"
    >
      <span>{{ name }}</span>
      <Info class="w-3 h-3 shrink-0" />
    </button>
    <Modal
      :open="open"
      :title="name"
      :subtitle="t('compare.expression_position', { index: detail.projection_index })"
      :close-label="t('compare.expression_close')"
      @close="close"
    >
      <div class="space-y-3">
        <div class="flex items-center gap-2 text-xs chrome-text-muted">
          <span>{{ t('compare.expression_original') }}</span>
          <span class="rounded-input px-1.5 py-0.5 chrome-accent-light-bg chrome-accent">
            {{ t('compare.expression_generated') }}
          </span>
        </div>
        <pre class="max-h-72 overflow-auto whitespace-pre rounded-card border chrome-border chrome-bg-elevated p-3 text-xs font-mono chrome-text-heading">{{ expression }}</pre>
        <div class="flex items-center gap-2">
          <button type="button" class="chrome-btn-secondary text-xs" @click="copyExpression">
            <Copy class="w-3.5 h-3.5" />
            {{ copied ? t('compare.expression_copied') : t('compare.expression_copy') }}
          </button>
          <span v-if="copyFailed" class="text-xs text-red-600 dark:text-red-400">
            {{ t('compare.expression_copy_failed') }}
          </span>
        </div>
      </div>
    </Modal>
  </template>
</template>
