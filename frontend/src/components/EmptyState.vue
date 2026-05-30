<script setup lang="ts">
import type { Component } from 'vue'

defineProps<{
  icon?: Component
  title: string
  hint?: string
}>()
</script>

<template>
  <div class="flex flex-col items-center text-center py-16 px-6">
    <div
      v-if="icon"
      class="w-14 h-14 rounded-card bg-sky-gradient-soft grid place-items-center mb-4 border"
    >
      <component :is="icon" class="w-6 h-6 text-sky-500 dark:text-sky-400" />
    </div>
    <div class="text-section font-semibold text-slate-700 dark:text-slate-200">{{ title }}</div>
    <div v-if="hint" class="text-sm text-slate-500 dark:text-slate-400 mt-1 max-w-sm">
      {{ hint }}
    </div>
    <slot />
  </div>
</template>
