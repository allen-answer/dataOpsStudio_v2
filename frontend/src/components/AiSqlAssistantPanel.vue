<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  generateSql,
  suggestSqlTables,
  type SqlDiagnosticCode,
  type SqlGenerateResponse,
  type SqlTableCandidate,
} from '../api/ai'
import { ApiError } from '../api/types'

const props = defineProps<{ open: boolean; datasourceId: string; editorSql: string }>()
const emit = defineEmits<{ apply: [sql: string]; close: [] }>()
const { t } = useI18n()

const prompt = ref('')
const candidates = ref<SqlTableCandidate[]>([])
const selected = ref(new Set<string>())
const preview = ref<SqlGenerateResponse | null>(null)
const revision = ref('')
const busy = ref<'candidates' | 'generate' | 'revise' | ''>('')
const errorCode = ref<SqlDiagnosticCode | null>(null)
const localErrorKey = ref<string | null>(null)
const diagnosticId = ref<string | null>(null)

const selectedCandidates = computed(() =>
  candidates.value.filter((item) => selected.value.has(candidateKey(item))),
)

function candidateKey(item: SqlTableCandidate): string {
  return item.schema_name ? `${item.schema_name}.${item.table_name}` : item.table_name
}

function reset(): void {
  prompt.value = ''
  candidates.value = []
  selected.value = new Set()
  preview.value = null
  revision.value = ''
  busy.value = ''
  errorCode.value = null
  localErrorKey.value = null
  diagnosticId.value = null
}

function clearError(): void {
  errorCode.value = null
  localErrorKey.value = null
  diagnosticId.value = null
}

function toggleCandidate(item: SqlTableCandidate, checked: boolean): void {
  const next = new Set(selected.value)
  if (checked) next.add(candidateKey(item))
  else next.delete(candidateKey(item))
  selected.value = next
}

function onCandidateChange(item: SqlTableCandidate, event: Event): void {
  toggleCandidate(item, (event.target as HTMLInputElement).checked)
}

async function recommend(): Promise<void> {
  if (!prompt.value.trim() || !props.datasourceId) return
  clearError()
  busy.value = 'candidates'
  try {
    const response = await suggestSqlTables(props.datasourceId, {
      natural_language: prompt.value.trim(),
      editor_sql: props.editorSql || null,
    })
    candidates.value = response.candidates
    selected.value = new Set(response.candidates.map(candidateKey))
    if (response.candidates.length === 0) localErrorKey.value = 'sql.ai_no_tables'
  } catch (error) {
    if (error instanceof ApiError && error.code === 'metadata_probe_failed') {
      errorCode.value = 'metadata_probe_failed'
    } else {
      localErrorKey.value = 'sql.ai_candidates_failed'
    }
  } finally {
    busy.value = ''
  }
}

async function requestPreview(isRevision: boolean): Promise<void> {
  clearError()
  const chosen = selectedCandidates.value
  if (chosen.length === 0) {
    localErrorKey.value = 'sql.ai_no_tables'
    return
  }
  const schemas = [...new Set(chosen.map((item) => item.schema_name))]
  if (schemas.length !== 1) {
    localErrorKey.value = 'sql.ai_tables_one_schema'
    return
  }
  if (isRevision && (!preview.value?.sql || !revision.value.trim())) return
  busy.value = isRevision ? 'revise' : 'generate'
  try {
    const response = await generateSql(props.datasourceId, {
      natural_language: prompt.value.trim(),
      schema_name: schemas[0],
      table_names: chosen.map((item) => item.table_name),
      candidate_sql: isRevision ? preview.value?.sql : undefined,
      revision_instruction: isRevision ? revision.value.trim() : undefined,
    })
    diagnosticId.value = response.request_id
    if (!response.ok || !response.sql) {
      if (!isRevision) preview.value = null
      errorCode.value = response.diagnostic_code ?? 'provider_invalid_response'
      return
    }
    preview.value = response
    revision.value = ''
  } catch (error) {
    errorCode.value =
      error instanceof ApiError && error.code === 'metadata_probe_failed'
        ? 'metadata_probe_failed'
        : 'provider_invalid_response'
  } finally {
    busy.value = ''
  }
}

function applyPreview(): void {
  if (preview.value?.sql) emit('apply', preview.value.sql)
}

async function copyPreview(): Promise<void> {
  if (preview.value?.sql) await navigator.clipboard.writeText(preview.value.sql)
}

watch(() => props.datasourceId, reset)
</script>

<template>
  <aside
    v-if="open"
    role="complementary"
    :aria-label="t('sql.ai_assistant_title')"
    class="fixed inset-y-0 right-0 z-40 w-[min(410px,100vw)] md:static md:z-auto md:w-[410px]
           shrink-0 min-w-0 max-w-full border-l chrome-border chrome-bg-panel
           flex flex-col overflow-hidden"
  >
    <header class="flex items-center justify-between border-b chrome-border px-4 py-3">
      <h2 class="font-semibold chrome-text-heading">{{ t('sql.ai_assistant_title') }}</h2>
      <button type="button" class="chrome-btn-ghost" :aria-label="t('common.close')" @click="emit('close')">×</button>
    </header>
    <div class="flex-1 overflow-y-auto p-4 space-y-4">
      <label class="block text-xs chrome-text-muted">
        {{ t('sql.ai_prompt_label') }}
        <textarea v-model="prompt" :aria-label="t('sql.ai_prompt_label')" class="chrome-input mt-1 w-full min-h-24" />
      </label>
      <button type="button" class="chrome-btn-secondary" :disabled="busy !== '' || !prompt.trim() || !datasourceId" @click="recommend">
        {{ busy === 'candidates' ? t('sql.ai_recommending_tables') : t('sql.ai_recommend_tables') }}
      </button>

      <fieldset v-if="candidates.length" class="space-y-2">
        <legend class="text-xs chrome-text-muted">{{ t('sql.ai_confirm_tables') }}</legend>
        <label v-for="item in candidates" :key="candidateKey(item)" class="flex items-center gap-2 text-sm">
          <input type="checkbox" :aria-label="candidateKey(item)" :checked="selected.has(candidateKey(item))" @change="onCandidateChange(item, $event)" />
          <span class="font-mono">{{ candidateKey(item) }}</span>
        </label>
      </fieldset>

      <button type="button" class="chrome-btn-primary" :disabled="busy !== '' || selectedCandidates.length === 0" @click="requestPreview(false)">
        {{ busy === 'generate' ? t('sql.ai_generating') : t('sql.ai_generate_preview') }}
      </button>

      <section v-if="preview?.sql" class="space-y-3">
        <pre class="max-w-full overflow-auto rounded-card chrome-bg-elevated p-3 text-xs"><code>{{ preview.sql }}</code></pre>
        <div v-if="preview.validation" class="flex flex-wrap gap-2 text-xs">
          <span>{{ t('sql.ai_validation_readonly') }}: {{ preview.validation.readonly }}</span>
          <span>{{ t('sql.ai_validation_tables') }}: {{ preview.validation.tables }}</span>
          <span>{{ t('sql.ai_validation_columns') }}: {{ preview.validation.columns }}</span>
        </div>
        <div class="flex gap-2">
          <button type="button" class="chrome-btn-primary" @click="applyPreview">{{ t('sql.ai_apply_editor') }}</button>
          <button type="button" class="chrome-btn-secondary" @click="copyPreview">{{ t('sql.ai_copy_sql') }}</button>
        </div>
        <label class="block text-xs chrome-text-muted">
          {{ t('sql.ai_revision_label') }}
          <textarea v-model="revision" :aria-label="t('sql.ai_revision_label')" :placeholder="t('sql.ai_revision_placeholder')" class="chrome-input mt-1 w-full min-h-16" />
        </label>
        <button type="button" class="chrome-btn-secondary" :disabled="busy !== '' || !revision.trim()" @click="requestPreview(true)">
          {{ busy === 'revise' ? t('sql.ai_generating') : t('sql.ai_revise_preview') }}
        </button>
      </section>

      <p v-if="localErrorKey" class="text-sm text-red-600">{{ t(localErrorKey) }}</p>
      <div v-if="errorCode" class="rounded-card border border-red-300 p-3 text-sm text-red-700">
        <p>{{ t(`sql.ai_diagnostic.${errorCode}`) }}</p>
        <p v-if="diagnosticId" class="mt-1 font-mono text-xs">{{ t('sql.ai_diagnostic_id', { id: diagnosticId }) }}</p>
      </div>
    </div>
  </aside>
</template>
