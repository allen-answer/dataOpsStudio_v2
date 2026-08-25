<script setup lang="ts">
/**
 * LineageDdlInput —— DDL 文本数据源输入(对标 DataGrip 的 DDL data source)。
 *
 * 血缘的 SQL 解析 / 批量分析两个入口共用:贴一段 CREATE TABLE 建表 DDL 补齐列
 * 元数据,不连真实数据库也能拿到列级血缘。留空 = 后端行为与从前完全一致。
 *
 * 只负责"收文本 + 回显上次解析摘要";DDL 怎么解析、和元数据缓存怎么合并全在后端
 * (app/domain/lineage/ddl_schema.py),前端不做任何解析。
 */
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { FileCode2, X } from 'lucide-vue-next'
import type { LineageDdlSchemaSummary } from '../api/lineage'

/** 与后端 app/api/schemas.py LINEAGE_DDL_MAX_CHARS 对齐(超限前端先拦,不等 422)。 */
const DDL_MAX_CHARS = 1_000_000

const props = defineProps<{
  modelValue: string
  disabled?: boolean
  /** 上一次解析的 DDL 摘要(后端回填);无则不显示徽标。 */
  summary?: LineageDdlSchemaSummary | null
}>()

const emit = defineEmits<{ 'update:modelValue': [string] }>()

const { t, te } = useI18n()

const fileInput = ref<HTMLInputElement | null>(null)
const error = ref<string | null>(null)

/**
 * 摘要文案。★ 跳过原因逐条展开,不合并成一个数字 —— 「3 条解析失败」和
 * 「3 条 GRANT」是完全不同的排查方向,混为一谈等于把用户往错误的方向推。
 * 未知的 reason 键宽容跳过(后端加新原因不会让前端崩)。
 */
const summaryText = computed<string>(() => {
  const value = props.summary
  if (!value) return ''
  if (value.error) {
    return t('lineage.ddl_dialect_unsupported', { dialect: value.dialect ?? value.error })
  }
  const parts = [
    t('lineage.ddl_applied', { tables: value.table_count, columns: value.column_count }),
  ]
  const shadowed = (value.parsed_table_count ?? value.table_count) - value.table_count
  if (shadowed > 0) parts.push(t('lineage.ddl_shadowed', { count: shadowed }))
  for (const [reason, count] of Object.entries(value.skipped_reasons ?? {})) {
    if (!count) continue
    const key = `lineage.ddl_skipped_${reason}`
    if (te(key)) parts.push(t(key, { count }))
  }
  if (value.failed_column_entry_count) {
    parts.push(t('lineage.ddl_partial_columns', { count: value.failed_column_entry_count }))
  }
  if (value.dialect) parts.push(t('lineage.ddl_dialect', { dialect: value.dialect }))
  return parts.join(' · ')
})

function setValue(next: string): void {
  if (next.length > DDL_MAX_CHARS) {
    error.value = t('lineage.ddl_too_large', { max: DDL_MAX_CHARS })
    return
  }
  error.value = null
  emit('update:modelValue', next)
}

function onInput(event: Event): void {
  setValue((event.target as HTMLTextAreaElement).value)
}

function openFilePicker(): void {
  if (!props.disabled) fileInput.value?.click()
}

// .sql 文件只在浏览器本地读成文本塞进输入框 —— 不走上传接口,后端只收文本。
async function onFileChange(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = '' // 允许再次选同名文件触发 change
  if (!file) return
  try {
    setValue(await file.text())
  } catch {
    error.value = t('lineage.ddl_read_failed')
  }
}
</script>

<template>
  <div class="block">
    <div class="flex flex-wrap items-center gap-2 mb-1">
      <span class="text-xs chrome-text-muted">{{ t('lineage.ddl_source') }}</span>
      <button
        type="button"
        class="chrome-btn-ghost text-[11px] px-1.5 py-0.5"
        :disabled="disabled"
        @click="openFilePicker"
      >
        <FileCode2 class="w-3.5 h-3.5" /> {{ t('lineage.ddl_load_file') }}
      </button>
      <button
        v-if="modelValue"
        type="button"
        class="chrome-btn-ghost text-[11px] px-1.5 py-0.5"
        :disabled="disabled"
        @click="setValue('')"
      >
        <X class="w-3.5 h-3.5" /> {{ t('lineage.ddl_clear') }}
      </button>
      <span
        v-if="summary"
        class="inline-flex items-center rounded-input px-1.5 py-0.5 text-[10px] font-medium"
        :class="
          summary.error
            ? 'bg-amber-50 text-amber-700 dark:bg-amber-500/15 dark:text-amber-300'
            : 'bg-emerald-50 text-emerald-700 dark:bg-emerald-500/15 dark:text-emerald-300'
        "
        data-testid="lineage-ddl-summary"
      >
        {{ summaryText }}
      </span>
    </div>
    <input
      ref="fileInput"
      type="file"
      class="hidden"
      accept=".sql,.txt,.ddl"
      data-testid="lineage-ddl-file"
      :disabled="disabled"
      @click.stop
      @change="onFileChange"
    />
    <textarea
      :value="modelValue"
      rows="5"
      class="chrome-input w-full text-sm font-mono"
      :disabled="disabled"
      :placeholder="t('lineage.ddl_ph')"
      data-testid="lineage-ddl-input"
      @input="onInput"
    />
    <p class="mt-1 text-[11px] chrome-text-muted">{{ t('lineage.ddl_hint') }}</p>
    <p v-if="error" class="mt-1 text-[11px] text-red-600 dark:text-red-400">{{ error }}</p>
  </div>
</template>
