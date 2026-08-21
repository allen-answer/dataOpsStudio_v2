<script setup lang="ts">
/**
 * AccountSecurityView —— /account/security(PRD §6)。
 *
 * 后端(app/api/routes/account.py):
 *   GET  /account/security                      → mfa_enabled / recovery_codes_total / used
 *   POST /account/password                      改密(401 invalid_password)
 *   POST /account/mfa/enroll                    → secret + otpauth_uri(前端转 QR)
 *   POST /account/mfa/verify                    验证 6 位 → enabled + 8 个恢复码(一次性)
 *   POST /account/mfa/disable                   关 MFA(需当前 6 位 TOTP)
 *   POST /account/recovery-codes/regenerate     重生成(需当前 6 位 TOTP,旧码全失效)
 *
 * ★ 这些 401(旧密码 / 当前 TOTP 不对)走 skipAuthRedirect,不把用户踢下线。
 * ★ 恢复码 / secret 只在响应里出现一次 —— 一次性展示 + 复制 / 下载,不缓存到任何持久层。
 */
import { computed, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useQuery, useMutation, useQueryClient } from '@tanstack/vue-query'
import {
  ShieldCheck,
  ShieldOff,
  KeyRound,
  Lock,
  AlertTriangle,
  CheckCircle2,
  Copy,
  Check,
  Download,
  RefreshCw,
} from 'lucide-vue-next'
import {
  getAccountSecurity,
  changeAccountPassword,
  enrollMfa,
  verifyMfa,
  disableMfa,
  regenerateRecoveryCodes,
} from '../api/account'
import { isSessionExpired, triggerUnauthenticated } from '../api/client'
import { ApiError, type AccountSecurityStatus } from '../api/types'
import LoadingDots from '../components/LoadingDots.vue'
import Modal from '../components/Modal.vue'
import QrCode from '../components/QrCode.vue'
import { createUserErrorMessage } from '../utils/userErrorMessage'

const { t } = useI18n()
const errorMessage = createUserErrorMessage(t)
const qc = useQueryClient()

const query = useQuery({ queryKey: ['account-security'], queryFn: getAccountSecurity })
const status = computed<AccountSecurityStatus | null>(() => query.data.value ?? null)

function invalidate(): Promise<void> {
  return qc.invalidateQueries({ queryKey: ['account-security'] })
}

// ─── 通用:复制 / 下载 恢复码 ────────────────────────────────
const copied = ref(false)
async function copyText(text: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text)
    copied.value = true
    setTimeout(() => (copied.value = false), 1500)
  } catch {
    /* clipboard 不可用时静默,用户仍可手动选中 */
  }
}
function downloadCodes(codes: string[]): void {
  const blob = new Blob([codes.join('\n') + '\n'], { type: 'text/plain' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = 'dataops-recovery-codes.txt'
  a.click()
  URL.revokeObjectURL(url)
}

// ─── 改密卡 ──────────────────────────────────────────────────
const pwForm = reactive({ old_password: '', new_password: '', confirm: '' })
const pwError = ref<string | null>(null)
const pwOk = ref(false)

const pwMutation = useMutation({
  mutationFn: () => changeAccountPassword(pwForm.old_password, pwForm.new_password),
  onSuccess: () => {
    pwOk.value = true
    pwForm.old_password = ''
    pwForm.new_password = ''
    pwForm.confirm = ''
    setTimeout(() => (pwOk.value = false), 3500)
  },
})

async function submitPassword(): Promise<void> {
  pwError.value = null
  pwOk.value = false
  if (!pwForm.old_password || !pwForm.new_password) {
    pwError.value = t('common.error_required_fields')
    return
  }
  if (pwForm.new_password !== pwForm.confirm) {
    pwError.value = t('account.pw_mismatch')
    return
  }
  try {
    await pwMutation.mutateAsync()
  } catch (e) {
    // skipAuthRedirect 端点:真会话过期(error=unauthorized)主动登出,不落业务文案。
    if (isSessionExpired(e)) {
      triggerUnauthenticated()
      return
    }
    if (e instanceof ApiError && e.code === 'invalid_password') {
      pwError.value = t('account.pw_invalid_old')
    } else {
      pwError.value = errorMessage(e)
    }
  }
}

// ─── MFA 卡 ─────────────────────────────────────────────────
// enroll → verify 子流程(modal)
const enrollOpen = ref(false)
const enrollSecret = ref<string | null>(null)
const enrollUri = ref<string | null>(null)
const enrollCode = ref('')
const enrollError = ref<string | null>(null)
const newRecoveryCodes = ref<string[] | null>(null) // 验证成功后一次性展示

const enrollMutation = useMutation({
  mutationFn: enrollMfa,
  onSuccess: (res) => {
    enrollSecret.value = res.secret
    enrollUri.value = res.otpauth_uri
  },
})

async function startEnroll(): Promise<void> {
  enrollSecret.value = null
  enrollUri.value = null
  enrollCode.value = ''
  enrollError.value = null
  newRecoveryCodes.value = null
  enrollOpen.value = true
  try {
    await enrollMutation.mutateAsync()
  } catch (e) {
    enrollError.value = errorMessage(e)
  }
}

const verifyMutation = useMutation({
  mutationFn: () => verifyMfa(enrollCode.value.trim()),
  onSuccess: async (res) => {
    newRecoveryCodes.value = res.recovery_codes
    await invalidate()
  },
})

async function submitVerify(): Promise<void> {
  enrollError.value = null
  if (!enrollCode.value.trim()) {
    enrollError.value = t('account.enter_code')
    return
  }
  try {
    await verifyMutation.mutateAsync()
  } catch (e) {
    if (isSessionExpired(e)) {
      triggerUnauthenticated()
      return
    }
    if (e instanceof ApiError && e.code === 'invalid_mfa_code') {
      enrollError.value = t('account.mfa_invalid_code')
    } else {
      enrollError.value = errorMessage(e)
    }
  }
}

function closeEnroll(): void {
  enrollOpen.value = false
  enrollSecret.value = null
  enrollUri.value = null
  enrollCode.value = ''
  newRecoveryCodes.value = null
}

// disable(modal,需当前 TOTP)
const disableOpen = ref(false)
const disableCode = ref('')
const disableError = ref<string | null>(null)

function openDisable(): void {
  disableCode.value = ''
  disableError.value = null
  disableOpen.value = true
}

const disableMutation = useMutation({
  mutationFn: () => disableMfa(disableCode.value.trim()),
  onSuccess: async () => {
    disableOpen.value = false
    await invalidate()
  },
})

async function submitDisable(): Promise<void> {
  disableError.value = null
  if (!disableCode.value.trim()) {
    disableError.value = t('account.enter_code')
    return
  }
  try {
    await disableMutation.mutateAsync()
  } catch (e) {
    if (isSessionExpired(e)) {
      triggerUnauthenticated()
      return
    }
    if (e instanceof ApiError && e.code === 'invalid_mfa_code') {
      disableError.value = t('account.mfa_invalid_code')
    } else {
      disableError.value = errorMessage(e)
    }
  }
}

// ─── 恢复码卡:regenerate(需当前 TOTP,二次确认)──────────────
const regenOpen = ref(false)
const regenCode = ref('')
const regenArmed = ref(false)
const regenError = ref<string | null>(null)
const regenResult = ref<string[] | null>(null)

function openRegen(): void {
  regenCode.value = ''
  regenArmed.value = false
  regenError.value = null
  regenResult.value = null
  regenOpen.value = true
}

const regenMutation = useMutation({
  mutationFn: () => regenerateRecoveryCodes(regenCode.value.trim()),
  onSuccess: async (res) => {
    regenResult.value = res.recovery_codes
    await invalidate()
  },
})

async function submitRegen(): Promise<void> {
  regenError.value = null
  if (!regenCode.value.trim()) {
    regenError.value = t('account.enter_code')
    return
  }
  if (!regenArmed.value) {
    regenArmed.value = true
    return
  }
  try {
    await regenMutation.mutateAsync()
  } catch (e) {
    if (isSessionExpired(e)) {
      triggerUnauthenticated()
      return
    }
    if (e instanceof ApiError && e.code === 'invalid_mfa_code') {
      regenError.value = t('account.mfa_invalid_code')
    } else {
      regenError.value = errorMessage(e)
    }
  }
}

const recoveryRemaining = computed(() => {
  if (!status.value) return 0
  return Math.max(0, status.value.recovery_codes_total - status.value.recovery_codes_used)
})
</script>

<template>
  <div class="max-w-2xl mx-auto px-6 lg:px-10 py-8 w-full">
    <div class="mb-6">
      <h1 class="text-h2 font-semibold tracking-tight chrome-text-heading">
        {{ t('account.title') }}
      </h1>
      <div class="text-sm chrome-text-muted mt-1">{{ t('account.subtitle') }}</div>
    </div>

    <div
      v-if="query.isLoading.value"
      class="flex items-center justify-center gap-2 py-12 text-sm chrome-text-muted"
    >
      <LoadingDots /><span>{{ t('common.loading') }}</span>
    </div>

    <div
      v-else-if="query.isError.value"
      class="border border-red-200 dark:border-red-500/30 bg-red-50 dark:bg-red-500/10 rounded-card p-5 flex items-start gap-3"
    >
      <AlertTriangle class="w-5 h-5 text-red-500 shrink-0 mt-0.5" />
      <div>
        <div class="text-sm font-medium text-red-700 dark:text-red-400">{{ t('common.error') }}</div>
        <button @click="query.refetch()" type="button" class="text-xs text-red-700 dark:text-red-400 underline mt-2">
          {{ t('common.retry') }}
        </button>
      </div>
    </div>

    <div v-else-if="status" class="space-y-5">
      <!-- ── 改密卡 ── -->
      <section class="chrome-bg-panel border chrome-border rounded-card p-5" style="box-shadow: var(--shadow-card);">
        <div class="flex items-center gap-2 mb-4">
          <Lock class="w-4 h-4 chrome-accent" />
          <h2 class="text-section font-semibold chrome-text-heading">{{ t('account.password_title') }}</h2>
        </div>
        <form @submit.prevent="submitPassword" class="space-y-3.5">
          <div class="space-y-1.5">
            <label class="form-label">{{ t('account.old_password') }}</label>
            <input v-model="pwForm.old_password" type="password" class="chrome-input w-full" autocomplete="current-password" :disabled="pwMutation.isPending.value" />
          </div>
          <div class="space-y-1.5">
            <label class="form-label">{{ t('account.new_password') }}</label>
            <input v-model="pwForm.new_password" type="password" class="chrome-input w-full" autocomplete="new-password" :disabled="pwMutation.isPending.value" />
          </div>
          <div class="space-y-1.5">
            <label class="form-label">{{ t('account.confirm_password') }}</label>
            <input v-model="pwForm.confirm" type="password" class="chrome-input w-full" autocomplete="new-password" :disabled="pwMutation.isPending.value" />
          </div>
          <div v-if="pwError" class="text-xs text-red-500">{{ pwError }}</div>
          <div v-if="pwOk" class="text-xs text-emerald-600 dark:text-emerald-400 inline-flex items-center gap-1">
            <CheckCircle2 class="w-3.5 h-3.5" /> {{ t('account.password_changed') }}
          </div>
          <div class="flex justify-end">
            <button type="submit" class="chrome-btn-primary" :disabled="pwMutation.isPending.value">
              <template v-if="pwMutation.isPending.value"><LoadingDots /><span>{{ t('common.submitting') }}</span></template>
              <span v-else>{{ t('account.change_password') }}</span>
            </button>
          </div>
        </form>
      </section>

      <!-- ── MFA 卡 ── -->
      <section class="chrome-bg-panel border chrome-border rounded-card p-5" style="box-shadow: var(--shadow-card);">
        <div class="flex items-center justify-between mb-4">
          <div class="flex items-center gap-2">
            <ShieldCheck class="w-4 h-4 chrome-accent" />
            <h2 class="text-section font-semibold chrome-text-heading">{{ t('account.mfa_title') }}</h2>
          </div>
          <span
            v-if="status.mfa_enabled"
            class="text-xs text-emerald-600 dark:text-emerald-400 inline-flex items-center gap-1 font-medium"
            data-testid="mfa-status-on"
          >
            <Check class="w-3.5 h-3.5" /> {{ t('account.mfa_on') }}
          </span>
          <span v-else class="text-xs chrome-text-muted" data-testid="mfa-status-off">{{ t('account.mfa_off') }}</span>
        </div>

        <p class="text-sm chrome-text-muted mb-4">{{ t('account.mfa_desc') }}</p>

        <div class="flex justify-end">
          <button v-if="!status.mfa_enabled" type="button" class="chrome-btn-primary" data-testid="mfa-enroll-btn" @click="startEnroll">
            <ShieldCheck class="w-4 h-4" />
            {{ t('account.mfa_enroll') }}
          </button>
          <button v-else type="button" class="chrome-btn-secondary hover:!text-red-500" data-testid="mfa-disable-btn" @click="openDisable">
            <ShieldOff class="w-4 h-4" />
            {{ t('account.mfa_disable') }}
          </button>
        </div>
      </section>

      <!-- ── 恢复码卡 ── -->
      <section class="chrome-bg-panel border chrome-border rounded-card p-5" style="box-shadow: var(--shadow-card);">
        <div class="flex items-center gap-2 mb-4">
          <KeyRound class="w-4 h-4 chrome-accent" />
          <h2 class="text-section font-semibold chrome-text-heading">{{ t('account.recovery_title') }}</h2>
        </div>

        <template v-if="status.mfa_enabled">
          <p class="text-sm chrome-text-muted mb-3">{{ t('account.recovery_desc') }}</p>
          <div class="flex items-center justify-between rounded-input chrome-bg-elevated px-3.5 py-2.5 mb-4">
            <span class="text-sm chrome-text-normal">{{ t('account.recovery_remaining') }}</span>
            <span class="text-sm font-mono font-semibold chrome-text-heading" data-testid="recovery-count">
              {{ recoveryRemaining }} / {{ status.recovery_codes_total }}
            </span>
          </div>
          <div class="flex justify-end">
            <button type="button" class="chrome-btn-secondary" data-testid="recovery-regen-btn" @click="openRegen">
              <RefreshCw class="w-4 h-4" />
              {{ t('account.recovery_regen') }}
            </button>
          </div>
        </template>
        <p v-else class="text-sm chrome-text-muted">{{ t('account.recovery_needs_mfa') }}</p>
      </section>
    </div>

    <!-- ── enroll modal(QR → verify → 恢复码)── -->
    <Modal :open="enrollOpen" :title="t('account.enroll_modal_title')" @close="closeEnroll">
      <!-- 阶段 3:验证成功 → 一次性展示恢复码 -->
      <div v-if="newRecoveryCodes" class="space-y-4" data-testid="enroll-recovery-codes">
        <div class="rounded-input border border-emerald-200 dark:border-emerald-500/30 bg-emerald-50 dark:bg-emerald-500/10 px-3 py-2.5 text-xs text-emerald-800 dark:text-emerald-300 inline-flex items-center gap-1.5">
          <CheckCircle2 class="w-4 h-4 shrink-0" /> {{ t('account.mfa_enabled_ok') }}
        </div>
        <div class="rounded-input border border-amber-200 dark:border-amber-500/30 bg-amber-50 dark:bg-amber-500/10 px-3 py-2.5 text-xs text-amber-800 dark:text-amber-300">
          {{ t('account.recovery_once_warning') }}
        </div>
        <div class="grid grid-cols-2 gap-2">
          <code
            v-for="code in newRecoveryCodes"
            :key="code"
            class="font-mono text-sm chrome-bg-elevated rounded-input px-2.5 py-1.5 text-center select-all chrome-text-heading"
          >
            {{ code }}
          </code>
        </div>
        <div class="flex justify-end gap-2 pt-1">
          <button type="button" class="chrome-btn-secondary" @click="copyText(newRecoveryCodes.join('\n'))">
            <component :is="copied ? Check : Copy" class="w-4 h-4" />
            {{ copied ? t('common.copied') : t('common.copy') }}
          </button>
          <button type="button" class="chrome-btn-secondary" @click="downloadCodes(newRecoveryCodes)">
            <Download class="w-4 h-4" /> {{ t('account.download_txt') }}
          </button>
          <button type="button" class="chrome-btn-primary" @click="closeEnroll">{{ t('common.done') }}</button>
        </div>
      </div>

      <!-- 阶段 1+2:展示 QR + secret,输入 6 位验证 -->
      <div v-else class="space-y-4">
        <div v-if="enrollMutation.isPending.value" class="flex items-center justify-center gap-2 py-8 text-sm chrome-text-muted">
          <LoadingDots /><span>{{ t('common.loading') }}</span>
        </div>
        <div v-else-if="enrollError && !enrollUri" class="text-xs text-red-500">{{ enrollError }}</div>
        <template v-else-if="enrollUri && enrollSecret">
          <p class="text-sm chrome-text-muted">{{ t('account.enroll_scan_hint') }}</p>
          <div class="flex justify-center">
            <QrCode :value="enrollUri" />
          </div>
          <div class="space-y-1.5">
            <label class="form-label">{{ t('account.secret_backup') }}</label>
            <code class="block chrome-input font-mono text-xs break-all select-all" data-testid="enroll-secret">{{ enrollSecret }}</code>
            <p class="text-[11px] chrome-text-muted">{{ t('account.secret_backup_hint') }}</p>
          </div>
          <form @submit.prevent="submitVerify" class="space-y-3 pt-1">
            <div class="space-y-1.5">
              <label class="form-label">{{ t('account.enter_6_digit') }}</label>
              <input
                v-model="enrollCode"
                type="text"
                inputmode="numeric"
                maxlength="6"
                placeholder="••••••"
                class="chrome-input w-full tracking-[0.4em] text-center font-mono text-lg"
                autocomplete="one-time-code"
                data-testid="enroll-code-input"
                :disabled="verifyMutation.isPending.value"
              />
            </div>
            <div v-if="enrollError" class="text-xs text-red-500">{{ enrollError }}</div>
            <div class="flex justify-end gap-2">
              <button type="button" class="chrome-btn-secondary" @click="closeEnroll" :disabled="verifyMutation.isPending.value">{{ t('common.cancel') }}</button>
              <button type="submit" class="chrome-btn-primary" :disabled="verifyMutation.isPending.value">
                <template v-if="verifyMutation.isPending.value"><LoadingDots /><span>{{ t('common.submitting') }}</span></template>
                <span v-else>{{ t('account.verify_enable') }}</span>
              </button>
            </div>
          </form>
        </template>
      </div>
    </Modal>

    <!-- ── disable modal ── -->
    <Modal :open="disableOpen" :title="t('account.disable_modal_title')" @close="disableOpen = false">
      <form @submit.prevent="submitDisable" class="space-y-4">
        <div class="rounded-input border border-amber-200 dark:border-amber-500/30 bg-amber-50 dark:bg-amber-500/10 px-3 py-2.5 text-xs text-amber-800 dark:text-amber-300">
          {{ t('account.disable_warning') }}
        </div>
        <div class="space-y-1.5">
          <label class="form-label">{{ t('account.current_totp') }}</label>
          <input
            v-model="disableCode"
            type="text"
            inputmode="numeric"
            maxlength="6"
            placeholder="••••••"
            class="chrome-input w-full tracking-[0.4em] text-center font-mono text-lg"
            autocomplete="one-time-code"
            data-testid="disable-code-input"
            :disabled="disableMutation.isPending.value"
          />
        </div>
        <div v-if="disableError" class="text-xs text-red-500">{{ disableError }}</div>
        <div class="flex justify-end gap-2">
          <button type="button" class="chrome-btn-secondary" @click="disableOpen = false" :disabled="disableMutation.isPending.value">{{ t('common.cancel') }}</button>
          <button type="submit" class="chrome-btn-primary chrome-btn-danger" :disabled="disableMutation.isPending.value">
            <template v-if="disableMutation.isPending.value"><LoadingDots /><span>{{ t('common.submitting') }}</span></template>
            <span v-else>{{ t('account.mfa_disable') }}</span>
          </button>
        </div>
      </form>
    </Modal>

    <!-- ── regenerate modal ── -->
    <Modal :open="regenOpen" :title="t('account.regen_modal_title')" @close="regenOpen = false">
      <!-- 成功 → 一次性展示新码 -->
      <div v-if="regenResult" class="space-y-4" data-testid="regen-result">
        <div class="rounded-input border border-amber-200 dark:border-amber-500/30 bg-amber-50 dark:bg-amber-500/10 px-3 py-2.5 text-xs text-amber-800 dark:text-amber-300">
          {{ t('account.recovery_once_warning') }}
        </div>
        <div class="grid grid-cols-2 gap-2">
          <code
            v-for="code in regenResult"
            :key="code"
            class="font-mono text-sm chrome-bg-elevated rounded-input px-2.5 py-1.5 text-center select-all chrome-text-heading"
          >
            {{ code }}
          </code>
        </div>
        <div class="flex justify-end gap-2 pt-1">
          <button type="button" class="chrome-btn-secondary" @click="copyText(regenResult.join('\n'))">
            <component :is="copied ? Check : Copy" class="w-4 h-4" />
            {{ copied ? t('common.copied') : t('common.copy') }}
          </button>
          <button type="button" class="chrome-btn-secondary" @click="downloadCodes(regenResult)">
            <Download class="w-4 h-4" /> {{ t('account.download_txt') }}
          </button>
          <button type="button" class="chrome-btn-primary" @click="regenOpen = false">{{ t('common.done') }}</button>
        </div>
      </div>

      <!-- 输入当前 TOTP -->
      <form v-else @submit.prevent="submitRegen" class="space-y-4">
        <div
          class="rounded-input px-3 py-2.5 text-xs flex items-start gap-2"
          style="background-color: rgb(239 68 68 / 0.08); color: rgb(185 28 28);"
        >
          <AlertTriangle class="w-4 h-4 shrink-0 mt-0.5" />
          <span>{{ regenArmed ? t('account.regen_armed') : t('account.regen_warning') }}</span>
        </div>
        <div class="space-y-1.5">
          <label class="form-label">{{ t('account.current_totp') }}</label>
          <input
            v-model="regenCode"
            type="text"
            inputmode="numeric"
            maxlength="6"
            placeholder="••••••"
            class="chrome-input w-full tracking-[0.4em] text-center font-mono text-lg"
            autocomplete="one-time-code"
            data-testid="regen-code-input"
            :disabled="regenMutation.isPending.value"
          />
        </div>
        <div v-if="regenError" class="text-xs text-red-500">{{ regenError }}</div>
        <div class="flex justify-end gap-2">
          <button type="button" class="chrome-btn-secondary" @click="regenOpen = false" :disabled="regenMutation.isPending.value">{{ t('common.cancel') }}</button>
          <button type="submit" class="chrome-btn-primary" :class="{ 'chrome-btn-danger': regenArmed }" :disabled="regenMutation.isPending.value">
            <template v-if="regenMutation.isPending.value"><LoadingDots /><span>{{ t('common.submitting') }}</span></template>
            <span v-else>{{ regenArmed ? t('account.regen_confirm') : t('account.recovery_regen') }}</span>
          </button>
        </div>
      </form>
    </Modal>
  </div>
</template>

<style scoped>
.form-label {
  @apply block text-xs uppercase tracking-wider font-medium;
  color: rgb(var(--text-muted));
}
</style>
