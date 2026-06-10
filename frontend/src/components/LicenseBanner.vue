<script setup lang="ts">
/**
 * LicenseBanner —— D.4 License 状态横条(顶部,全局)。
 *
 * | mode     | 颜色          | 文案 |
 * | VALID    | (不显示)      | — |
 * | TRIAL    | sky 浅底      | 试用版,剩余 N 天 |
 * | IN_GRACE | amber 浅底    | License 即将过期,剩余 N 天,请尽快更新 |
 * | EXPIRED  | red 浅底      | License 已过期,系统功能受限 |
 * | REPAIR   | red 深底      | ★ 系统进入维护模式,仅允许 license 更新 |
 *
 * 不接 chrome accent —— license 状态是语义色(像 job 状态点四色硬绑),与 variant 无关。
 */
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { AlertTriangle, Clock, ShieldAlert } from 'lucide-vue-next'
import { useLicense } from '../composables/useLicense'

const { t } = useI18n()
const { status, mode, showBanner, isRepair, isExpired, isInGrace, isTrial } = useLicense()

const days = computed(() => status.value?.trial_days_remaining ?? 0)

const message = computed(() => {
  if (isRepair.value) return t('license.banner_repair')
  if (isExpired.value) return t('license.banner_expired')
  if (isInGrace.value) return t('license.banner_in_grace', { days: days.value })
  if (isTrial.value) return t('license.banner_trial', { days: days.value })
  return ''
})

// 横条样式按 mode(语义色,不走 variant token)
const barClass = computed(() => {
  switch (mode.value) {
    case 'repair':
      return 'bg-red-600 text-white border-red-700'
    case 'expired':
      return 'bg-red-50 text-red-700 border-red-200 dark:bg-red-500/10 dark:text-red-300 dark:border-red-500/30'
    case 'in_grace':
      return 'bg-amber-50 text-amber-800 border-amber-200 dark:bg-amber-500/10 dark:text-amber-300 dark:border-amber-500/30'
    case 'trial':
      return 'bg-sky-50 text-sky-700 border-sky-200 dark:bg-sky-500/10 dark:text-sky-300 dark:border-sky-500/30'
    default:
      return ''
  }
})

const icon = computed(() => {
  if (isRepair.value) return ShieldAlert
  if (isExpired.value || isInGrace.value) return AlertTriangle
  return Clock
})
</script>

<template>
  <div
    v-if="showBanner"
    class="flex items-center justify-center gap-2 px-4 py-1.5 text-xs font-medium border-b"
    :class="barClass"
    role="status"
  >
    <component :is="icon" class="w-3.5 h-3.5 shrink-0" />
    <span>{{ message }}</span>
  </div>
</template>
