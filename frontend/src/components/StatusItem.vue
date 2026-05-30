<script setup lang="ts">
import { computed } from 'vue'
import StatusDot from './StatusDot.vue'

type Status = 'pending' | 'running' | 'success' | 'failed' | 'cancelled'

const props = defineProps<{ status: Status }>()

const label = computed(
  () =>
    ({
      pending: 'Pending',
      running: 'Running',
      success: 'Success',
      failed: 'Failed',
      cancelled: 'Cancelled',
    })[props.status],
)
</script>

<template>
  <div class="flex items-center gap-2.5 py-1">
    <StatusDot :status="status" />
    <div class="flex flex-col leading-tight">
      <span class="text-sm font-medium text-slate-800 dark:text-slate-100">{{ label }}</span>
      <span class="text-xs text-slate-500 dark:text-slate-400 font-mono">{{ status }}</span>
    </div>
  </div>
</template>
