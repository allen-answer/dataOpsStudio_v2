<script setup lang="ts">
/**
 * ConsoleSessionBar —— SQL 工作台的会话状态条(Session Broker 设计 §3.3「体验闭环」)。
 *
 * 一条横条同时承担三件事,按严重度**互斥**呈现:
 *  1. 状态条 —— 会话态点 + 文案 + 硬取消能力 + 关闭按钮(正常态)
 *  2. 接管提示 —— 本 tab 的 epoch 落后于会话当前 epoch(M1 双 tab),
 *     此时本 tab 既不能提交也不能取消,只能「重新连接」抢回持有权
 *  3. session_lost banner —— 会话不可续用,「重新连接」重建
 *
 * `server_cancel=degraded` 时必须明说「取消将断开会话」——
 * 设计 §4.1:任何一步失败都如实上报,绝不让用户以为取消是无损的。
 *
 * 无会话(懒 attach 未触发 / 回退 job 路径)时整条不渲染 —— 不给用户造概念。
 */
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { AlertTriangle, Link2Off, Plug, RefreshCw, ShieldAlert, X } from 'lucide-vue-next'
import type { ConsoleSessionState } from '../composables/useConsoleSessions'

const props = defineProps<{
  session: ConsoleSessionState | null
  takenOver: boolean
  lost: boolean
  busy: boolean
}>()

const emit = defineEmits<{ (e: 'reconnect'): void; (e: 'close'): void }>()

const { t } = useI18n()

const visible = computed(() => Boolean(props.session?.sessionId || props.session?.attaching))

/** 严重度优先:接管 > 丢失 > 正常。 */
const tone = computed<'takeover' | 'lost' | 'normal'>(() => {
  if (props.takenOver) return 'takeover'
  if (props.lost) return 'lost'
  return 'normal'
})

const barClass = computed(() => {
  switch (tone.value) {
    case 'takeover':
      return 'bg-amber-50 text-amber-800 border-amber-200 dark:bg-amber-500/10 dark:text-amber-300 dark:border-amber-500/30'
    case 'lost':
      return 'bg-red-50 text-red-700 border-red-200 dark:bg-red-500/10 dark:text-red-300 dark:border-red-500/30'
    default:
      return 'chrome-border-subtle chrome-text-muted'
  }
})

const dotClass = computed(() => {
  switch (props.session?.state) {
    case 'executing':
    case 'cancelling':
      return 'bg-sky-500 animate-pulse-soft'
    case 'idle':
      return 'bg-emerald-500'
    case 'connecting':
    case 'closing':
      return 'bg-slate-400 animate-pulse-soft'
    default:
      return 'bg-slate-400'
  }
})

/** 会话态文案。attach 在途时会话还没有 state,单独给一句。 */
const stateLabel = computed(() => {
  const session = props.session
  if (!session) return ''
  if (session.attaching && !session.state) return t('sql.session_state_connecting')
  switch (session.state) {
    case 'connecting':
      return t('sql.session_state_connecting')
    case 'idle':
      return t('sql.session_state_idle')
    case 'executing':
      return t('sql.session_state_executing')
    case 'cancelling':
      return t('sql.session_state_cancelling')
    case 'closing':
      return t('sql.session_state_closing')
    case 'closed':
      return t('sql.session_state_closed')
    case 'session_lost':
      return t('sql.session_state_lost')
    case 'connect_failed':
      return t('sql.session_state_connect_failed')
    default:
      return ''
  }
})

/** 丢失/关闭的归因:close_reason 与 error_code 都是非敏感的运维口径值。 */
const reasonLabel = computed(() => {
  const session = props.session
  if (!session) return ''
  const reason = session.closeReason ?? session.errorCode
  return reason ? t('sql.session_reason', { reason }) : ''
})

const degraded = computed(() => props.session?.serverCancel === 'degraded')
</script>

<template>
  <div
    v-if="visible"
    data-testid="sql-session-bar"
    :data-session-state="session?.state ?? 'connecting'"
    :data-session-tone="tone"
    class="flex items-center gap-2 px-5 py-1.5 text-xs border-b"
    :class="barClass"
    role="status"
  >
    <template v-if="tone === 'takeover'">
      <AlertTriangle class="w-3.5 h-3.5 shrink-0" />
      <span data-testid="sql-session-takeover" class="min-w-0 truncate">
        {{ t('sql.session_taken_over') }}
      </span>
    </template>
    <template v-else-if="tone === 'lost'">
      <Link2Off class="w-3.5 h-3.5 shrink-0" />
      <span data-testid="sql-session-lost" class="min-w-0 truncate">
        {{ t('sql.session_lost_banner') }}{{ reasonLabel }}
      </span>
    </template>
    <template v-else>
      <Plug class="w-3.5 h-3.5 shrink-0" />
      <span class="relative inline-block w-2 h-2 rounded-full shrink-0" :class="dotClass"></span>
      <span data-testid="sql-session-state" class="min-w-0 truncate">{{ stateLabel }}</span>
      <span v-if="session?.dbType" class="chrome-text-muted shrink-0">· {{ session.dbType }}</span>
    </template>

    <span
      v-if="degraded && tone === 'normal'"
      data-testid="sql-session-cancel-degraded"
      class="flex items-center gap-1 shrink-0 text-amber-700 dark:text-amber-300"
      :title="t('sql.session_cancel_degraded_hint')"
    >
      <ShieldAlert class="w-3.5 h-3.5 shrink-0" />
      {{ t('sql.session_cancel_degraded') }}
    </span>

    <span v-if="session?.error" class="min-w-0 truncate text-red-600 dark:text-red-400">
      {{ session.error }}
    </span>

    <div class="flex-1" />

    <button
      v-if="tone !== 'normal'"
      type="button"
      data-testid="sql-session-reconnect"
      class="chrome-btn-secondary shrink-0 whitespace-nowrap"
      :disabled="busy"
      @click="emit('reconnect')"
    >
      <RefreshCw class="w-3.5 h-3.5" />
      {{ t('sql.session_reconnect') }}
    </button>
    <button
      v-else
      type="button"
      data-testid="sql-session-close"
      class="chrome-btn-ghost shrink-0 whitespace-nowrap"
      :disabled="busy"
      :title="t('sql.session_close_hint')"
      @click="emit('close')"
    >
      <X class="w-3.5 h-3.5" />
      {{ t('sql.session_close') }}
    </button>
  </div>
</template>
