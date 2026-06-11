<script setup lang="ts">
/**
 * JobErrorBadge —— job 失败/取消/超时时的结构化错误码徽标。
 *
 * 输入:结构化 error_code(优先)+ 旧 job 的 error 字符串(回落)。
 * 经 resolveJobError 映射到 i18n 文案 + tone 配色;无错误信息则不渲染。
 * 不暴露 driver raw error(R2),只展示分类文案。
 */
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { AlertCircle, Clock, Ban } from 'lucide-vue-next'
import type { JobErrorCode } from '../api/types'
import { JOB_ERROR_TONE_CLASS, resolveJobError } from '../api/jobError'

const props = defineProps<{
  errorCode?: JobErrorCode | null
  error?: string | null
}>()

const { t } = useI18n()

const display = computed(() => resolveJobError(props.errorCode, props.error))

const TONE_ICON = {
  red: AlertCircle,
  amber: Clock,
  slate: Ban,
} as const
</script>

<template>
  <span
    v-if="display"
    class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-input text-xs font-medium border whitespace-nowrap"
    :class="JOB_ERROR_TONE_CLASS[display.tone]"
  >
    <component :is="TONE_ICON[display.tone]" class="w-3 h-3 shrink-0" />
    {{ t(display.i18nKey) }}
  </span>
</template>
