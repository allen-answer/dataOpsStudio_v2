<script setup lang="ts">
import { Check, ChevronDown, Search } from 'lucide-vue-next'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import type { MetadataTableItem } from '../api/metadata'

type MatchMode = 'fuzzy' | 'exact'

const props = withDefaults(
  defineProps<{
    modelValue: string
    options: readonly MetadataTableItem[]
    loading?: boolean
    disabled?: boolean
    testId: string
  }>(),
  {
    loading: false,
    disabled: false,
  },
)

const emit = defineEmits<{
  'update:modelValue': [value: string]
}>()

const { t } = useI18n()
const root = ref<HTMLElement | null>(null)
const trigger = ref<HTMLButtonElement | null>(null)
const searchInput = ref<HTMLInputElement | null>(null)
const open = ref(false)
const search = ref('')
const mode = ref<MatchMode>('fuzzy')
const activeIndex = ref(0)
const RENDER_LIMIT = 200

const uniqueOptions = computed(() => {
  const seen = new Set<string>()
  return props.options.filter((option) => {
    if (seen.has(option.name)) return false
    seen.add(option.name)
    return true
  })
})

function fuzzyScore(candidate: string, query: string): number | null {
  if (candidate === query) return -3000
  if (candidate.startsWith(query)) return -2000 + candidate.length - query.length
  const substringIndex = candidate.indexOf(query)
  if (substringIndex >= 0) {
    return -1000 + substringIndex * 10 + candidate.length - query.length
  }

  let cursor = 0
  let previous = -1
  let gaps = 0
  for (const character of query) {
    const index = candidate.indexOf(character, cursor)
    if (index < 0) return null
    if (previous >= 0) gaps += index - previous - 1
    previous = index
    cursor = index + 1
  }
  return gaps * 10 + candidate.length
}

const matchedOptions = computed(() => {
  const query = search.value.trim().toLocaleLowerCase()
  if (!query) return uniqueOptions.value
  if (mode.value === 'exact') {
    return uniqueOptions.value.filter((option) => option.name.toLocaleLowerCase() === query)
  }
  return uniqueOptions.value
    .map((option, index) => ({
      option,
      index,
      score: fuzzyScore(option.name.toLocaleLowerCase(), query),
    }))
    .filter((item): item is typeof item & { score: number } => item.score !== null)
    .sort((left, right) => left.score - right.score || left.index - right.index)
    .map((item) => item.option)
})

const visibleOptions = computed(() => {
  const matches = matchedOptions.value
  if (search.value.trim()) return matches.slice(0, RENDER_LIMIT)
  const current = matches.find((option) => option.name === props.modelValue)
  if (!current) return matches.slice(0, RENDER_LIMIT)
  return [current, ...matches.filter((option) => option.name !== current.name)].slice(
    0,
    RENDER_LIMIT,
  )
})

const activeOptionId = computed(() =>
  visibleOptions.value[activeIndex.value] ? `${props.testId}-option-${activeIndex.value}` : undefined,
)

function scrollActiveOptionIntoView(): void {
  void nextTick(() => {
    if (!activeOptionId.value) return
    document.getElementById(activeOptionId.value)?.scrollIntoView({ block: 'nearest' })
  })
}

function show(): void {
  if (props.disabled) return
  open.value = true
  activeIndex.value = Math.max(
    0,
    visibleOptions.value.findIndex((option) => option.name === props.modelValue),
  )
  void nextTick(() => {
    searchInput.value?.focus()
    scrollActiveOptionIntoView()
  })
}

function close(restoreFocus = false): void {
  open.value = false
  search.value = ''
  activeIndex.value = 0
  if (restoreFocus) void nextTick(() => trigger.value?.focus())
}

function choose(value: string): void {
  emit('update:modelValue', value)
  close(true)
}

function moveActive(delta: number): void {
  const length = visibleOptions.value.length
  if (length === 0) return
  activeIndex.value = (activeIndex.value + delta + length) % length
  scrollActiveOptionIntoView()
}

function onSearchKeydown(event: KeyboardEvent): void {
  if (event.key === 'ArrowDown') {
    event.preventDefault()
    moveActive(1)
  } else if (event.key === 'ArrowUp') {
    event.preventDefault()
    moveActive(-1)
  } else if (event.key === 'Enter') {
    event.preventDefault()
    const option = visibleOptions.value[activeIndex.value]
    if (option) choose(option.name)
  }
}

function onDocumentPointerDown(event: PointerEvent): void {
  if (root.value?.contains(event.target as Node)) return
  close(false)
}

function onFocusOut(event: FocusEvent): void {
  const next = event.relatedTarget
  if (next instanceof Node && root.value?.contains(next)) return
  close(false)
}

watch([visibleOptions, mode], () => {
  activeIndex.value = 0
  scrollActiveOptionIntoView()
})

watch(
  () => props.disabled,
  (disabled) => {
    if (disabled) close()
  },
)

onMounted(() => document.addEventListener('pointerdown', onDocumentPointerDown))
onBeforeUnmount(() => document.removeEventListener('pointerdown', onDocumentPointerDown))
</script>

<template>
  <div ref="root" class="relative min-w-0" @focusout="onFocusOut">
    <button
      ref="trigger"
      type="button"
      aria-haspopup="listbox"
      :aria-expanded="open"
      :aria-controls="`${testId}-options`"
      :data-testid="testId"
      :data-value="modelValue"
      class="chrome-input w-full text-sm flex items-center justify-between gap-2 text-left disabled:opacity-60 disabled:cursor-not-allowed"
      :disabled="disabled"
      @click="open ? close(false) : show()"
      @keydown.down.prevent="show"
      @keydown.enter.prevent="open ? close(false) : show()"
      @keydown.esc.prevent="close(false)"
    >
      <span class="min-w-0 flex-1 truncate" :class="!modelValue && 'chrome-text-muted'">
        {{ modelValue || t('compare.table') }}
      </span>
      <ChevronDown class="w-3.5 h-3.5 shrink-0 chrome-text-muted" :class="open && 'rotate-180'" />
    </button>

    <div
      v-if="open"
      class="absolute z-50 mt-1 w-full min-w-[18rem] overflow-hidden rounded-card border chrome-border chrome-bg-panel"
      style="box-shadow: var(--shadow-card);"
      @keydown.esc.stop.prevent="close(true)"
    >
      <div class="p-2 space-y-2 border-b chrome-border-subtle">
        <label class="relative block">
          <Search class="absolute left-2 top-1/2 -translate-y-1/2 w-3.5 h-3.5 chrome-text-muted" />
          <input
            ref="searchInput"
            v-model="search"
            role="combobox"
            aria-autocomplete="list"
            aria-expanded="true"
            :aria-controls="`${testId}-options`"
            :aria-activedescendant="activeOptionId"
            :data-testid="`${testId}-search`"
            class="chrome-input w-full pl-7 pr-2 text-xs"
            :placeholder="t('compare.table_search')"
            autocomplete="off"
            @keydown="onSearchKeydown"
          />
        </label>
        <div class="flex items-center justify-between gap-2">
          <span class="text-[10px] chrome-text-muted">{{ t('compare.table_match_mode') }}</span>
          <div class="inline-flex rounded-input border chrome-border-subtle p-0.5">
            <button
              type="button"
              :data-testid="`${testId}-mode-fuzzy`"
              class="px-2 py-0.5 rounded text-[10px] transition-colors"
              :class="mode === 'fuzzy' ? 'chrome-accent-light-bg chrome-accent' : 'chrome-text-muted'"
              :aria-pressed="mode === 'fuzzy'"
              :title="t('compare.table_match_fuzzy_hint')"
              @click="mode = 'fuzzy'"
            >
              {{ t('compare.table_match_fuzzy') }}
            </button>
            <button
              type="button"
              :data-testid="`${testId}-mode-exact`"
              class="px-2 py-0.5 rounded text-[10px] transition-colors"
              :class="mode === 'exact' ? 'chrome-accent-light-bg chrome-accent' : 'chrome-text-muted'"
              :aria-pressed="mode === 'exact'"
              :title="t('compare.table_match_exact_hint')"
              @click="mode = 'exact'"
            >
              {{ t('compare.table_match_exact') }}
            </button>
          </div>
        </div>
        <p class="text-[10px] chrome-text-muted">
          {{
            t('compare.table_search_count', {
              shown: visibleOptions.length,
              matched: matchedOptions.length,
              total: uniqueOptions.length,
            })
          }}
        </p>
      </div>

      <div
        :id="`${testId}-options`"
        :data-testid="`${testId}-options`"
        role="listbox"
        :aria-label="t('compare.table')"
        class="max-h-64 overflow-y-auto p-1"
      >
        <div v-if="loading && uniqueOptions.length === 0" class="px-2 py-3 text-xs chrome-text-muted">
          {{ t('common.loading') }}
        </div>
        <div v-else-if="visibleOptions.length === 0" class="px-2 py-3 text-xs chrome-text-muted">
          {{ t('compare.table_search_empty') }}
        </div>
        <button
          v-for="(table, index) in visibleOptions"
          :key="table.name"
          :id="`${testId}-option-${index}`"
          type="button"
          role="option"
          tabindex="-1"
          :aria-selected="table.name === modelValue"
          class="w-full flex items-center gap-2 rounded-input px-2 py-1.5 text-left text-xs font-mono transition-colors"
          :class="[
            index === activeIndex ? 'chrome-accent-light-bg' : 'hover:chrome-bg-elevated',
            table.name === modelValue ? 'chrome-accent' : 'chrome-text-heading',
          ]"
          @mouseenter="activeIndex = index"
          @click="choose(table.name)"
        >
          <Check class="w-3.5 h-3.5 shrink-0" :class="table.name === modelValue ? 'opacity-100' : 'opacity-0'" />
          <span class="min-w-0 flex-1 truncate" :title="table.name">{{ table.name }}</span>
        </button>
      </div>
    </div>
  </div>
</template>
