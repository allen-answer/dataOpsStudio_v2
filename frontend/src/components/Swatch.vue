<script setup lang="ts">
defineProps<{
  color: string
  name: string
  hex: string
  usage?: string
}>()
</script>

<template>
  <div
    class="bg-white dark:bg-slate-800/60 border border-slate-200 dark:border-slate-700 rounded-card overflow-hidden shadow-subtle"
  >
    <div class="h-16" :class="color"></div>
    <div class="p-3">
      <div class="flex items-baseline justify-between">
        <span class="text-xs font-mono font-medium text-slate-800 dark:text-slate-100">{{
          name
        }}</span>
        <span class="text-xs font-mono text-slate-400 dark:text-slate-500">{{ hex }}</span>
      </div>
      <div v-if="usage" class="text-xs text-slate-500 dark:text-slate-400 mt-1 leading-snug">
        {{ usage }}
      </div>
    </div>
  </div>
</template>
