<script setup lang="ts">
/** 全局 toast 渲染宿主 —— 挂在 App.vue 根部,右下角堆叠。 */
import { CheckCircle2, AlertTriangle, Info, X } from 'lucide-vue-next'
import { useToast } from '../composables/useToast'

const { toasts, dismiss } = useToast()

const ICON = { success: CheckCircle2, error: AlertTriangle, info: Info } as const
const ACCENT: Record<string, string> = {
  success: 'text-emerald-500',
  error: 'text-red-500',
  info: 'chrome-accent',
}
</script>

<template>
  <div class="fixed bottom-5 right-5 z-[60] flex flex-col gap-2 pointer-events-none">
    <TransitionGroup
      enter-active-class="transition duration-200 ease-out"
      enter-from-class="opacity-0 translate-y-2"
      enter-to-class="opacity-100 translate-y-0"
      leave-active-class="transition duration-150 ease-in"
      leave-from-class="opacity-100"
      leave-to-class="opacity-0 translate-y-1"
    >
      <div
        v-for="item in toasts"
        :key="item.id"
        role="status"
        data-testid="toast"
        class="pointer-events-auto flex items-start gap-2.5 max-w-sm chrome-bg-panel border chrome-border rounded-card px-3.5 py-2.5 shadow-card"
      >
        <component :is="ICON[item.kind]" class="w-4 h-4 shrink-0 mt-0.5" :class="ACCENT[item.kind]" />
        <span class="text-sm chrome-text-normal flex-1">{{ item.message }}</span>
        <button type="button" class="chrome-btn-ghost -mr-1 -mt-0.5" @click="dismiss(item.id)">
          <X class="w-3.5 h-3.5" />
        </button>
      </div>
    </TransitionGroup>
  </div>
</template>
