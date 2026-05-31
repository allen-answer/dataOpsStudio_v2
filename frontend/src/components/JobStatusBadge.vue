<script setup lang="ts">
/**
 * JobStatusBadge —— job 状态徽标,色用 TokensView §1 状态点锚:
 *   pending   → slate 灰
 *   running   → sky pulse(进行中)
 *   success   → emerald
 *   failed    → red
 *   cancelled → slate
 *   timeout   → amber
 *
 * 不接 chrome accent —— 这是数据语义色(像 §3 Compare 4 桶一样四色硬绑),
 * 跟 variant 无关。
 */
import { computed } from 'vue'
import {
  CheckCircle2,
  XCircle,
  Loader2,
  Pause,
  Clock,
  Hourglass,
} from 'lucide-vue-next'
import { useI18n } from 'vue-i18n'
import type { JobStatus } from '../api/types'

const props = withDefaults(
  defineProps<{
    status: JobStatus
    /** sm = 行内紧凑;md = 卡片标题(略大)*/
    size?: 'sm' | 'md'
  }>(),
  { size: 'sm' },
)

const { t } = useI18n()

interface Style {
  icon: typeof CheckCircle2
  color: string
  spin?: boolean
  pulse?: boolean
}

const STYLE_MAP: Record<JobStatus, Style> = {
  pending: {
    icon: Hourglass,
    color: 'bg-slate-100 text-slate-600 dark:bg-slate-500/15 dark:text-slate-300 border-slate-200/60 dark:border-slate-500/30',
  },
  running: {
    icon: Loader2,
    color: 'bg-sky-50 text-sky-700 dark:bg-sky-500/15 dark:text-sky-300 border-sky-200/60 dark:border-sky-500/30',
    spin: true,
  },
  success: {
    icon: CheckCircle2,
    color: 'bg-emerald-50 text-emerald-700 dark:bg-emerald-500/15 dark:text-emerald-300 border-emerald-200/60 dark:border-emerald-500/30',
  },
  failed: {
    icon: XCircle,
    color: 'bg-red-50 text-red-700 dark:bg-red-500/15 dark:text-red-300 border-red-200/60 dark:border-red-500/30',
  },
  cancelled: {
    icon: Pause,
    color: 'bg-slate-100 text-slate-600 dark:bg-slate-500/15 dark:text-slate-300 border-slate-200/60 dark:border-slate-500/30',
  },
  timeout: {
    icon: Clock,
    color: 'bg-amber-50 text-amber-700 dark:bg-amber-500/15 dark:text-amber-300 border-amber-200/60 dark:border-amber-500/30',
  },
}

const style = computed(() => STYLE_MAP[props.status])
const label = computed(() => t(`jobs.status_${props.status}`))

const sizeClass = computed(() =>
  props.size === 'md'
    ? 'gap-1.5 px-2 py-0.5 text-xs'
    : 'gap-1 px-1.5 py-0.5 text-xs',
)
const iconSize = computed(() => (props.size === 'md' ? 'w-3.5 h-3.5' : 'w-3 h-3'))
</script>

<template>
  <span
    class="inline-flex items-center rounded-input font-medium border whitespace-nowrap"
    :class="[style.color, sizeClass]"
  >
    <component
      :is="style.icon"
      :class="[iconSize, style.spin && 'animate-spin']"
    />
    {{ label }}
  </span>
</template>
