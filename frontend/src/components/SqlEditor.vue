<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import * as monaco from 'monaco-editor/esm/vs/editor/editor.api'
import 'monaco-editor/esm/vs/basic-languages/sql/sql.contribution'
import 'monaco-editor/esm/vs/editor/contrib/suggest/browser/suggestController.js'
import 'monaco-editor/esm/vs/editor/contrib/hover/browser/hoverContribution.js'
import editorWorker from 'monaco-editor/esm/vs/editor/editor.worker?worker'
import { VueMonacoEditor, loader } from '@guolao/vue-monaco-editor'
import { Maximize2, Minimize2 } from 'lucide-vue-next'
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
    /** opt-in:显示放大按钮 + 全屏模态编辑。默认关闭,其他页面行为不变。 */
    expandable?: boolean
    /** 放大层可访问名称 / 按钮文案,由调用方用 i18n 传入。 */
    expandLabel?: string
    collapseLabel?: string
    expandedTitle?: string
    /** 稳定 data-testid 前缀,只在 expandable 时用于放大按钮 / 模态。 */
    testid?: string
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
    expandable: false,
    expandLabel: 'Expand editor',
    collapseLabel: 'Restore editor',
    expandedTitle: 'SQL editor',
    testid: 'sql-editor',
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

let inlineIntelligence: monaco.IDisposable | null = null
let modalIntelligence: monaco.IDisposable | null = null
let inlineEditor: monaco.editor.IStandaloneCodeEditor | null = null
let modalEditor: monaco.editor.IStandaloneCodeEditor | null = null

const expanded = ref(false)
const modalRoot = ref<HTMLElement | null>(null)
const expandButton = ref<HTMLButtonElement | null>(null)

/** 放大 / 还原之间搬运的光标与滚动状态。 */
type ViewState = {
  selection: monaco.Selection | null
  position: monaco.Position | null
  scrollTop: number
  scrollLeft: number
}
let pendingViewState: ViewState | null = null

function captureViewState(editor: monaco.editor.IStandaloneCodeEditor | null): ViewState | null {
  if (!editor) return null
  return {
    selection: editor.getSelection(),
    position: editor.getPosition(),
    scrollTop: editor.getScrollTop(),
    scrollLeft: editor.getScrollLeft(),
  }
}

function applyViewState(
  editor: monaco.editor.IStandaloneCodeEditor | null,
  state: ViewState | null,
): void {
  if (!editor || !state) return
  if (state.selection) editor.setSelection(state.selection)
  else if (state.position) editor.setPosition(state.position)
  editor.setScrollTop(state.scrollTop)
  editor.setScrollLeft(state.scrollLeft)
}

const baseOptions = computed<monaco.editor.IStandaloneEditorConstructionOptions>(() => ({
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

const editorOptions = baseOptions
const modalEditorOptions = computed<monaco.editor.IStandaloneEditorConstructionOptions>(() => ({
  ...baseOptions.value,
  ariaLabel: props.expandedTitle,
}))

/** inline / modal 共享同一 Monaco model,保留 undo/redo 历史;view state 仍分别搬运。 */
const modalPath = computed(() => props.path)

const expandTestid = computed(() => props.testid + '-expand')
const overlayTestid = computed(() => props.testid + '-expand-overlay')
const modalTestid = computed(() => props.testid + '-expand-modal')
const collapseTestid = computed(() => props.testid + '-collapse')

const rootStyle = computed(() => ({
  height: props.height,
  minHeight: `${Math.max(3, props.minLines) * 1.25 + 1.5}rem`,
}))

function bindEditor(editor: monaco.editor.IStandaloneCodeEditor): monaco.IDisposable {
  const disposable = attachSqlIntelligence(editor, {
    datasourceId: () => props.datasourceId,
    dbType: () => props.dbType || undefined,
    defaultSchema: () => props.defaultSchema || undefined,
  })
  editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.Enter, () => emit('execute'))
  return disposable
}

function onMount(editor: monaco.editor.IStandaloneCodeEditor): void {
  inlineIntelligence?.dispose()
  inlineEditor = editor
  inlineIntelligence = bindEditor(editor)
  emit('mount', editor)
}

function onModalMount(editor: monaco.editor.IStandaloneCodeEditor): void {
  modalIntelligence?.dispose()
  modalEditor = editor
  modalIntelligence = bindEditor(editor)
  if (expanded.value) restoreModalView()
}

function onValueChange(value: string): void {
  if (value !== props.modelValue) emit('update:modelValue', value)
}

function openExpanded(): void {
  if (!props.expandable || expanded.value) return
  pendingViewState = captureViewState(inlineEditor)
  expanded.value = true
  void nextTick(restoreModalView)
}

function restoreModalView(): void {
  modalEditor?.layout()
  applyViewState(modalEditor, pendingViewState)
  pendingViewState = null
  modalEditor?.focus()
}

function closeExpanded(): void {
  if (!expanded.value) return
  pendingViewState = captureViewState(modalEditor)
  expanded.value = false
  void nextTick(() => {
    applyViewState(inlineEditor, pendingViewState)
    pendingViewState = null
    expandButton.value?.focus()
  })
}

function onWindowKeydown(event: KeyboardEvent): void {
  if (event.key !== 'Escape') return
  event.stopPropagation()
  closeExpanded()
}

function onDialogKeydown(event: KeyboardEvent): void {
  if (event.key !== 'Tab') return
  const root = modalRoot.value
  if (!root) return
  const focusable = Array.from(
    root.querySelectorAll<HTMLElement>(
      'button:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ),
  ).filter((element) => element.getClientRects().length > 0)
  if (focusable.length === 0) {
    event.preventDefault()
    return
  }
  const first = focusable[0]
  const last = focusable[focusable.length - 1]
  const active = document.activeElement
  if (event.shiftKey && (active === first || !root.contains(active))) {
    event.preventDefault()
    last?.focus()
  } else if (!event.shiftKey && (active === last || !root.contains(active))) {
    event.preventDefault()
    first?.focus()
  }
}

watch(expanded, (open) => {
  if (open) window.addEventListener('keydown', onWindowKeydown)
  else window.removeEventListener('keydown', onWindowKeydown)
})

onUnmounted(() => {
  window.removeEventListener('keydown', onWindowKeydown)
  inlineIntelligence?.dispose()
  inlineIntelligence = null
  modalIntelligence?.dispose()
  modalIntelligence = null
  inlineEditor = null
  modalEditor = null
})
</script>

<template>
  <div class="relative min-w-0 overflow-hidden" :style="rootStyle">
    <VueMonacoEditor
      :value="modelValue"
      language="sql"
      :path="path"
      :theme="theme"
      :options="editorOptions"
      @update:value="onValueChange"
      @mount="onMount"
    />
    <button
      v-if="expandable"
      ref="expandButton"
      type="button"
      class="absolute right-1.5 top-1.5 z-10 rounded-card border chrome-border chrome-bg-panel p-1 chrome-text-muted hover:chrome-text-heading"
      :data-testid="expandTestid"
      :title="expandLabel"
      :aria-label="expandLabel"
      @click="openExpanded"
    >
      <Maximize2 class="w-3.5 h-3.5" />
    </button>

    <Teleport to="body">
      <div
        v-if="expandable"
        v-show="expanded"
        class="fixed inset-0 z-[60] flex items-center justify-center bg-black/50 p-4"
        :data-testid="overlayTestid"
        @click.self="closeExpanded"
      >
        <div
          ref="modalRoot"
          role="dialog"
          aria-modal="true"
          :aria-label="expandedTitle"
          :data-testid="modalTestid"
          class="flex h-[92vh] w-[96vw] flex-col rounded-card border chrome-border chrome-bg-panel shadow-subtle"
          @keydown="onDialogKeydown"
        >
          <div class="flex items-center justify-between border-b chrome-border px-3 py-2">
            <span class="text-base font-medium chrome-text-heading">{{ expandedTitle }}</span>
            <button
              type="button"
              class="chrome-btn-secondary text-sm"
              :data-testid="collapseTestid"
              :title="collapseLabel"
              :aria-label="collapseLabel"
              @click="closeExpanded"
            >
              <Minimize2 class="w-3.5 h-3.5" /> {{ collapseLabel }}
            </button>
          </div>
          <div class="min-h-0 flex-1 overflow-hidden">
            <VueMonacoEditor
              :value="modelValue"
              language="sql"
              :path="modalPath"
              :theme="theme"
              :options="modalEditorOptions"
              @update:value="onValueChange"
              @mount="onModalMount"
            />
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>
