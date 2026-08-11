<script setup lang="ts">
import { computed, onUnmounted } from 'vue'
import * as monaco from 'monaco-editor/esm/vs/editor/editor.api'
import 'monaco-editor/esm/vs/basic-languages/sql/sql.contribution'
import 'monaco-editor/esm/vs/editor/contrib/suggest/browser/suggestController.js'
import 'monaco-editor/esm/vs/editor/contrib/hover/browser/hoverContribution.js'
import editorWorker from 'monaco-editor/esm/vs/editor/editor.worker?worker'
import { VueMonacoEditor, loader } from '@guolao/vue-monaco-editor'
import { attachSqlIntelligence } from '../utils/sqlIntelligence'

const props = withDefaults(
  defineProps<{
    modelValue: string
    datasourceId?: string
    dbType?: string
    defaultSchema?: string
    theme?: string
    readOnly?: boolean
    path?: string
    placeholder?: string
    height?: string
    fontSize?: number
    minLines?: number
  }>(),
  {
    datasourceId: '',
    dbType: '',
    defaultSchema: '',
    theme: 'vs',
    readOnly: false,
    path: undefined,
    placeholder: '',
    height: '100%',
    fontSize: 13,
    minLines: 5,
  },
)

const emit = defineEmits<{
  'update:modelValue': [value: string]
  mount: [editor: monaco.editor.IStandaloneCodeEditor]
  execute: []
}>()

const globalScope = self as unknown as { MonacoEnvironment?: { getWorker: () => Worker } }
if (!globalScope.MonacoEnvironment) {
  globalScope.MonacoEnvironment = { getWorker: () => new editorWorker() }
}
loader.config({ monaco })

let intelligence: monaco.IDisposable | null = null

const editorOptions = computed<monaco.editor.IStandaloneEditorConstructionOptions>(() => ({
  automaticLayout: true,
  fontSize: props.fontSize,
  fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
  minimap: { enabled: false },
  scrollBeyondLastLine: false,
  tabSize: 2,
  wordWrap: 'on',
  renderLineHighlight: 'gutter',
  padding: { top: 12, bottom: 12 },
  readOnly: props.readOnly,
  fixedOverflowWidgets: true,
  suggest: {
    showKeywords: true,
    showFields: true,
    showClasses: true,
    snippetsPreventQuickSuggestions: false,
  },
  quickSuggestions: { other: true, comments: false, strings: false },
  suggestOnTriggerCharacters: true,
  hover: { enabled: true, delay: 250 },
  ariaLabel: 'SQL editor',
  placeholder: props.placeholder,
}))

const rootStyle = computed(() => ({
  height: props.height,
  minHeight: `${Math.max(3, props.minLines) * 1.25 + 1.5}rem`,
}))

function onMount(editor: monaco.editor.IStandaloneCodeEditor): void {
  intelligence?.dispose()
  intelligence = attachSqlIntelligence(editor, {
    datasourceId: () => props.datasourceId,
    dbType: () => props.dbType || undefined,
    defaultSchema: () => props.defaultSchema || undefined,
  })
  editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.Enter, () => emit('execute'))
  emit('mount', editor)
}

function onValueChange(value: string): void {
  if (value !== props.modelValue) emit('update:modelValue', value)
}

onUnmounted(() => {
  intelligence?.dispose()
  intelligence = null
})
</script>

<template>
  <div class="min-w-0 overflow-hidden" :style="rootStyle">
    <VueMonacoEditor
      :value="modelValue"
      language="sql"
      :path="path"
      :theme="theme"
      :options="editorOptions"
      @update:value="onValueChange"
      @mount="onMount"
    />
  </div>
</template>
