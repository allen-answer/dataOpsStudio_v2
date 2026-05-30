<script setup lang="ts">
defineProps<{
  index: number
  title: string
  hint?: string
}>()
</script>

<template>
  <div class="flex items-baseline gap-3 border-b border-slate-200 dark:border-slate-700 pb-2">
    <span class="text-xs font-mono text-slate-400 dark:text-slate-500 tabular-nums">§{{ index }}</span>
    <h2 class="text-h2 font-semibold tracking-tight text-slate-800 dark:text-slate-100">{{ title }}</h2>
    <span v-if="hint" class="text-xs text-slate-500 dark:text-slate-400 ml-auto">{{ hint }}</span>
  </div>
</template>
