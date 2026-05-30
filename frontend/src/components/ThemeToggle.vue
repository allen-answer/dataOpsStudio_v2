<script setup lang="ts">
import { Sun, Moon } from 'lucide-vue-next'

export type Theme = 'light' | 'dark'

defineProps<{ modelValue: Theme }>()
const emit = defineEmits<{ (e: 'update:modelValue', v: Theme): void }>()

function toggle(): void {
  emit('update:modelValue', 'light')
}
function setDark(): void {
  emit('update:modelValue', 'dark')
}
function setLight(): void {
  toggle()
}
</script>

<template>
  <div class="inline-flex items-center rounded-md border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800 p-0.5">
    <button
      type="button"
      @click="setLight"
      class="px-2 py-1 rounded text-xs font-medium transition-all flex items-center gap-1"
      :class="
        modelValue === 'light'
          ? 'bg-sky-50 text-sky-700 dark:bg-sky-500/20 dark:text-sky-300'
          : 'text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-200'
      "
      aria-label="Light theme"
    >
      <Sun class="w-3.5 h-3.5" />
      Light
    </button>
    <button
      type="button"
      @click="setDark"
      class="px-2 py-1 rounded text-xs font-medium transition-all flex items-center gap-1"
      :class="
        modelValue === 'dark'
          ? 'bg-sky-50 text-sky-700 dark:bg-sky-500/20 dark:text-sky-300'
          : 'text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-200'
      "
      aria-label="Dark theme"
    >
      <Moon class="w-3.5 h-3.5" />
      Dark
    </button>
  </div>
</template>
