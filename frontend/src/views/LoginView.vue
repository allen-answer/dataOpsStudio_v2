<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { Sparkles, ShieldCheck, ArrowLeft } from 'lucide-vue-next'
import { storeToRefs } from 'pinia'
import { useAuthStore } from '../stores/auth'
import { useThemeStore } from '../stores/theme'
import { variants } from '../variants'
import { ApiError } from '../api/types'
import LoadingDots from '../components/LoadingDots.vue'

const { t } = useI18n()
const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

const themeStore = useThemeStore()
const { variant } = storeToRefs(themeStore)
const accentHex = computed(
  () => variants.find((v) => v.id === variant.value)?.accentHex ?? '#0ea5e9',
)

const username = ref('')
const password = ref('')
const submitting = ref(false)
const errorMessage = ref<string | null>(null)
const usernameRef = ref<HTMLInputElement | null>(null)

/**
 * 第二因子状态机:
 *   step='credentials' → 用户名 + 密码;若后端 401 mfa_required → 切到 step='mfa'
 *   step='mfa'         → 6 位 TOTP(或恢复码模式);401 invalid_mfa_code 明确报错
 * 非 MFA 用户永远停在 step='credentials',流程不变。
 */
const step = ref<'credentials' | 'mfa'>('credentials')
const mfaCode = ref('')
const useRecoveryCode = ref(false) // false=TOTP 6 位,true=恢复码
const mfaCodeRef = ref<HTMLInputElement | null>(null)

const sessionExpired = computed(() => route.query.reason === 'expired')
const nextPath = computed(() =>
  typeof route.query.next === 'string' ? route.query.next : null,
)

onMounted(() => {
  usernameRef.value?.focus()
})

function backToCredentials(): void {
  step.value = 'credentials'
  mfaCode.value = ''
  useRecoveryCode.value = false
  errorMessage.value = null
  void nextTick(() => usernameRef.value?.focus())
}

function toggleRecoveryMode(): void {
  useRecoveryCode.value = !useRecoveryCode.value
  mfaCode.value = ''
  errorMessage.value = null
  void nextTick(() => mfaCodeRef.value?.focus())
}

async function doLogin(): Promise<void> {
  await auth.login(
    username.value.trim(),
    password.value,
    step.value === 'mfa' ? mfaCode.value.trim() : undefined,
  )
  await router.push(
    nextPath.value && nextPath.value.startsWith('/') ? nextPath.value : '/projects',
  )
}

async function onSubmit(): Promise<void> {
  errorMessage.value = null

  if (step.value === 'credentials') {
    if (!username.value.trim()) {
      errorMessage.value = t('auth.error_empty_username')
      return
    }
    if (!password.value) {
      errorMessage.value = t('auth.error_empty_password')
      return
    }
  } else if (!mfaCode.value.trim()) {
    errorMessage.value = t('auth.error_empty_mfa')
    return
  }

  submitting.value = true
  try {
    await doLogin()
  } catch (e) {
    if (e instanceof ApiError && e.code === 'mfa_required') {
      // 密码对了,需要第二因子 → 进第二步(不报错)。
      step.value = 'mfa'
      errorMessage.value = null
      void nextTick(() => mfaCodeRef.value?.focus())
    } else if (e instanceof ApiError && e.code === 'invalid_mfa_code') {
      errorMessage.value = t('auth.error_invalid_mfa')
    } else if (e instanceof ApiError && e.status === 401) {
      // invalid_credentials —— 回到第一步语义(若在第二步,密码侧也可能失效)。
      errorMessage.value = t('auth.error_invalid_credentials')
    } else if (e instanceof ApiError && e.status === 0) {
      errorMessage.value = t('common.error_network')
    } else if (e instanceof ApiError && e.status >= 500) {
      errorMessage.value = t('common.error_server')
    } else if (e instanceof ApiError) {
      errorMessage.value = e.message || t('common.error_unknown')
    } else {
      errorMessage.value = t('common.error_unknown')
    }
  } finally {
    submitting.value = false
  }
}

// 星座节点 & 边(viewBox 0-100)。节点 18-22 为 accent 簇。
// 灵感:Figma Make LoginView.tsx 的 Constellation;此处为手工裁出的可复现版本。
interface Node { x: number; y: number; r: number; accent?: boolean }

const NODES: Node[] = [
  { x: 8, y: 14, r: 1.0 }, { x: 18, y: 8, r: 1.2 }, { x: 28, y: 20, r: 0.9 },
  { x: 38, y: 12, r: 1.0 }, { x: 50, y: 6, r: 1.1 }, { x: 62, y: 16, r: 0.9 },
  { x: 75, y: 10, r: 1.0 }, { x: 88, y: 20, r: 1.1 },
  { x: 12, y: 30, r: 0.9 }, { x: 25, y: 36, r: 1.0 }, { x: 42, y: 28, r: 0.8 },
  { x: 55, y: 34, r: 1.1 }, { x: 68, y: 26, r: 0.9 }, { x: 80, y: 38, r: 1.0 },
  { x: 92, y: 30, r: 0.8 },
  { x: 6, y: 46, r: 1.0 }, { x: 20, y: 52, r: 0.9 }, { x: 35, y: 48, r: 1.0 },
  // 18-22 accent 簇
  { x: 48, y: 52, r: 1.8, accent: true },
  { x: 52, y: 46, r: 1.5, accent: true },
  { x: 56, y: 54, r: 1.6, accent: true },
  { x: 44, y: 58, r: 1.4, accent: true },
  { x: 50, y: 60, r: 1.3, accent: true },
  { x: 65, y: 48, r: 1.0 }, { x: 78, y: 56, r: 0.9 }, { x: 90, y: 48, r: 1.1 },
  { x: 10, y: 66, r: 0.9 }, { x: 22, y: 72, r: 1.0 }, { x: 35, y: 76, r: 0.8 },
  { x: 48, y: 78, r: 1.1 }, { x: 60, y: 70, r: 0.9 }, { x: 72, y: 80, r: 1.0 },
  { x: 85, y: 72, r: 0.8 }, { x: 95, y: 64, r: 1.0 },
  { x: 5, y: 84, r: 0.9 }, { x: 18, y: 90, r: 1.0 }, { x: 32, y: 88, r: 0.8 },
  { x: 45, y: 92, r: 1.1 }, { x: 58, y: 85, r: 0.9 }, { x: 70, y: 92, r: 1.0 },
  { x: 82, y: 88, r: 0.8 }, { x: 92, y: 84, r: 0.9 },
]

type EdgeT = [number, number]

const EDGES: EdgeT[] = [
  [0, 1], [1, 2], [2, 3], [3, 4], [4, 5], [5, 6], [6, 7],
  [0, 8], [1, 9], [3, 10], [4, 10], [5, 11], [6, 12], [7, 13], [7, 14],
  [8, 9], [9, 10], [10, 11], [11, 12], [12, 13], [13, 14],
  [8, 15], [9, 16], [10, 17],
  [15, 16], [16, 17],
  [17, 18], [17, 21], [10, 18], [11, 19], [11, 20], [12, 23], [13, 24],
  [18, 19], [18, 20], [18, 21], [19, 20], [19, 22], [20, 22], [21, 22],
  [20, 23], [22, 29], [21, 28],
  [23, 24], [24, 25], [13, 24], [14, 25],
  [26, 27], [27, 28], [28, 29], [29, 30], [30, 31], [31, 32], [32, 33],
  [15, 26], [16, 27], [17, 28], [23, 30], [24, 31], [25, 33],
  [26, 34], [27, 35], [28, 36], [29, 37], [30, 38], [31, 39], [32, 40], [33, 41],
  [34, 35], [35, 36], [36, 37], [37, 38], [38, 39], [39, 40], [40, 41],
]

const accentNodes = computed(() => NODES.filter((n) => n.accent))
</script>

<template>
  <div class="min-h-full flex-1 flex chrome-bg-main relative overflow-hidden">
    <!-- 左侧:星座 + 品牌(md+) -->
    <div class="hidden md:flex flex-col relative w-1/2 overflow-hidden chrome-bg-panel">
      <!-- dot grid -->
      <div
        class="absolute inset-0"
        style="background-image: radial-gradient(rgb(var(--text-muted) / 0.18) 1px, transparent 1px); background-size: 18px 18px;"
      />

      <!-- 星座 -->
      <svg
        class="absolute inset-0 w-full h-full"
        viewBox="0 0 100 100"
        preserveAspectRatio="xMidYMid slice"
        aria-hidden="true"
      >
        <g :stroke="accentHex" stroke-width="0.12" stroke-opacity="0.35">
          <line
            v-for="(e, i) in EDGES"
            :key="`e-${i}`"
            :x1="NODES[e[0]].x"
            :y1="NODES[e[0]].y"
            :x2="NODES[e[1]].x"
            :y2="NODES[e[1]].y"
          />
        </g>
        <g>
          <circle
            v-for="(n, i) in accentNodes"
            :key="`g-${i}`"
            :cx="n.x"
            :cy="n.y"
            :r="n.r * 1.8"
            :fill="accentHex"
            fill-opacity="0.18"
          />
          <circle
            v-for="(n, i) in NODES"
            :key="`n-${i}`"
            :cx="n.x"
            :cy="n.y"
            :r="n.r * 0.55"
            :fill="n.accent ? accentHex : 'rgb(var(--text-muted))'"
            :fill-opacity="n.accent ? 0.95 : 0.45"
          />
        </g>
      </svg>

      <!-- vignette -->
      <div
        class="absolute inset-0 pointer-events-none"
        style="background: radial-gradient(ellipse at center, transparent 35%, rgb(var(--bg-main) / 0.55) 100%);"
      />

      <!-- brand 左下 -->
      <div class="relative z-10 mt-auto mb-12 ml-12 max-w-xs">
        <div class="flex items-center gap-3 mb-3">
          <div
            class="w-10 h-10 rounded-card grid place-items-center shadow-card"
            style="background-image: var(--gradient-brand);"
          >
            <Sparkles class="w-5 h-5 text-white" />
          </div>
          <div class="chrome-text-heading text-lg font-semibold tracking-tight">
            DataOps Studio
          </div>
        </div>
        <div class="chrome-text-muted text-sm leading-relaxed">
          {{ t('auth.tagline') }}
        </div>
      </div>

      <!-- 右下版本 -->
      <div class="absolute bottom-6 right-6 z-10 text-xs chrome-text-muted font-mono tracking-wider">
        v2.0 · {{ variants.find((v) => v.id === variant)?.name }}
      </div>
    </div>

    <!-- 右侧:表单 -->
    <div class="flex-1 flex items-center justify-center px-6 py-12">
      <div class="w-full max-w-sm">
        <!-- 小屏 brand -->
        <div class="md:hidden flex flex-col items-center mb-8">
          <div
            class="w-12 h-12 rounded-card grid place-items-center shadow-card mb-3"
            style="background-image: var(--gradient-brand);"
          >
            <Sparkles class="w-6 h-6 text-white" />
          </div>
          <div class="chrome-text-heading text-base font-semibold tracking-tight">
            DataOps Studio
          </div>
        </div>

        <div class="mb-7">
          <h1 class="chrome-text-heading text-xl font-semibold tracking-tight">
            {{ step === 'mfa' ? t('auth.mfa_title') : t('auth.login_title') }}
          </h1>
          <div class="chrome-text-muted text-sm mt-1">
            {{ step === 'mfa' ? t('auth.mfa_subtitle') : t('auth.login_subtitle') }}
          </div>
        </div>

        <form @submit.prevent="onSubmit" class="space-y-4">
          <div
            v-if="sessionExpired && step === 'credentials'"
            class="px-3 py-2 rounded-input border text-xs"
            style="background-color: rgb(var(--accent) / 0.08); border-color: rgb(var(--accent) / 0.30); color: rgb(var(--accent));"
          >
            {{ t('common.error_unauthorized') }}
          </div>

          <!-- ── 第一步:用户名 + 密码 ── -->
          <template v-if="step === 'credentials'">
            <div class="space-y-1.5">
              <label class="block text-xs uppercase tracking-wider chrome-text-muted font-medium">
                {{ t('auth.username') }}
              </label>
              <input
                ref="usernameRef"
                v-model="username"
                type="text"
                class="chrome-input w-full"
                autocomplete="username"
                :disabled="submitting"
              />
            </div>

            <div class="space-y-1.5">
              <label class="block text-xs uppercase tracking-wider chrome-text-muted font-medium">
                {{ t('auth.password') }}
              </label>
              <input
                v-model="password"
                type="password"
                class="chrome-input w-full"
                autocomplete="current-password"
                :disabled="submitting"
              />
            </div>
          </template>

          <!-- ── 第二步:TOTP / 恢复码 ── -->
          <template v-else>
            <div
              class="flex items-center gap-2.5 px-3 py-2.5 rounded-input border text-xs"
              style="background-color: rgb(var(--accent) / 0.07); border-color: rgb(var(--accent) / 0.25); color: rgb(var(--accent));"
            >
              <ShieldCheck class="w-4 h-4 shrink-0" />
              <span>{{ t('auth.mfa_hint') }}</span>
            </div>

            <div class="space-y-1.5">
              <label class="block text-xs uppercase tracking-wider chrome-text-muted font-medium">
                {{ useRecoveryCode ? t('auth.recovery_code') : t('auth.mfa_code') }}
              </label>
              <input
                ref="mfaCodeRef"
                v-model="mfaCode"
                type="text"
                :inputmode="useRecoveryCode ? 'text' : 'numeric'"
                :maxlength="useRecoveryCode ? 64 : 6"
                :placeholder="useRecoveryCode ? t('auth.recovery_code_ph') : '••••••'"
                class="chrome-input w-full"
                :class="{ 'tracking-[0.4em] text-center font-mono text-lg': !useRecoveryCode }"
                autocomplete="one-time-code"
                :disabled="submitting"
                data-testid="mfa-code-input"
              />
            </div>

            <button
              type="button"
              class="text-xs chrome-accent hover:underline"
              :disabled="submitting"
              data-testid="mfa-toggle-recovery"
              @click="toggleRecoveryMode"
            >
              {{ useRecoveryCode ? t('auth.use_totp') : t('auth.use_recovery') }}
            </button>
          </template>

          <div
            v-if="errorMessage"
            class="text-xs"
            style="color: #ef4444;"
            role="alert"
          >
            {{ errorMessage }}
          </div>

          <button
            type="submit"
            class="chrome-btn-primary w-full justify-center"
            :disabled="submitting"
          >
            <template v-if="submitting">
              <LoadingDots />
              <span>{{ step === 'mfa' ? t('auth.verifying') : t('auth.submitting') }}</span>
            </template>
            <span v-else>{{ step === 'mfa' ? t('auth.mfa_submit') : t('auth.submit') }}</span>
          </button>

          <button
            v-if="step === 'mfa'"
            type="button"
            class="w-full inline-flex items-center justify-center gap-1.5 text-xs chrome-accent hover:underline"
            :disabled="submitting"
            @click="backToCredentials"
          >
            <ArrowLeft class="w-3.5 h-3.5" />
            {{ t('auth.mfa_back') }}
          </button>
        </form>

        <div class="text-center text-xs chrome-text-muted mt-6 font-mono">
          DataOps Studio · 2.0
        </div>
      </div>
    </div>
  </div>
</template>
