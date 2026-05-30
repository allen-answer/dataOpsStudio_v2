<script setup lang="ts">
import { computed } from 'vue'

type Status = 'pending' | 'running' | 'success' | 'failed' | 'cancelled'

const props = defineProps<{ status: Status }>()

const dotColor = computed(
  () =>
    ({
      pending: 'bg-slate-400',
      running: 'bg-sky-500',
      success: 'bg-emerald-500',
      failed: 'bg-red-500',
      cancelled: 'bg-slate-400',
    })[props.status],
)

const isRunning = computed(() => props.status === 'running')
</script>

<template>
  <span class="relative inline-flex w-2 h-2">
    <span
      v-if="isRunning"
      class="absolute inset-0 rounded-full animate-pulse-ring"
      :class="dotColor"
    ></span>
    <span
      class="relative inline-block w-2 h-2 rounded-full"
      :class="[dotColor, isRunning ? 'animate-pulse-soft' : '']"
    ></span>
  </span>
</template>
