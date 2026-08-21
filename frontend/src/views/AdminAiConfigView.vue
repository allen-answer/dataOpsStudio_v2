<script setup lang="ts">
/**
 * AdminAiConfigView —— /admin/ai-config(PRD §9 + ADR-0016)。
 *
 * 后端(app/api/routes/admin.py):
 *   GET  /admin/ai-config        → AdminAiConfigResponse(key 永不回显)
 *   PUT  /admin/ai-config         保存配置(api_key 与 clear_api_key 互斥)
 *   POST /admin/ai-config/test    用已落库配置测试连接
 *
 * 2.0.x 实现约束(后端 _validate_ai_config_update):
 *   - 仅 mock / openai_compatible 可 enabled;anthropic / ollama enabled 会 400 unsupported_provider
 *     → 前端把这俩标"未实现"并在选中且开启时禁用保存。
 *   - openai_compatible 开启时必须有 base_url。
 *   - l4_requires_optin 后端恒 true(传 false → 400);出站等级最高只能设到 L3(le=3),
 *     L4 行永久锁定灰显(tooltip 引 ADR-0016)。
 *
 * test 失败结构化 error:ai_disabled / unsupported_provider / missing_provider_config
 *   / 或 AiGatewayError 子类名(后端 type(exc).__name__,如 AiGatewayTimeout)。
 */
import { computed, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useQuery, useMutation, useQueryClient } from '@tanstack/vue-query'
import {
  Bot,
  AlertTriangle,
  CheckCircle2,
  Lock,
  Plug,
  Trash2,
  KeyRound,
} from 'lucide-vue-next'
import { getAdminAiConfig, putAdminAiConfig, testAdminAiConfig } from '../api/admin'
import {
  ApiError,
  type AdminAiConfigResponse,
  type AdminAiConfigTestResponse,
  type AiProvider,
} from '../api/types'
import LoadingDots from '../components/LoadingDots.vue'
import { createUserErrorMessage } from '../utils/userErrorMessage'

const { t } = useI18n()
const errorMessage = createUserErrorMessage(t)
const qc = useQueryClient()

const query = useQuery({ queryKey: ['admin-ai-config'], queryFn: getAdminAiConfig })

// provider 元数据:2.0.x 仅 mock / openai_compatible 可用;其余标"未实现"。
const PROVIDERS: { id: AiProvider; implemented: boolean }[] = [
  { id: 'off', implemented: true },
  { id: 'mock', implemented: true },
  { id: 'openai_compatible', implemented: true },
  { id: 'anthropic', implemented: false },
  { id: 'ollama', implemented: false },
]

// 出站等级 L0–L3 单选 + L4 永久锁定(ADR-0016)。
const EGRESS_LEVELS = [
  { level: 0, dot: 'bg-slate-400' },
  { level: 1, dot: 'bg-slate-400' },
  { level: 2, dot: 'bg-sky-500' },
  { level: 3, dot: 'bg-amber-500' },
] as const

// ─── 表单本地态(从服务端 hydrate)──────────────────────────
const form = reactive({
  enabled: false,
  provider: 'off' as AiProvider,
  model: '',
  base_url: '',
  api_key: '',
  clear_api_key: false,
  max_auto_egress_level: 0,
  enable_inference: false,
  enable_auto_translation: false,
})
// 服务端当前 key 状态(只读展示;新输入 / 清除前的基线)
const apiKeySource = ref<'none' | 'stored' | 'env'>('none')
const hasStoredKey = ref(false)

function hydrate(cfg: AdminAiConfigResponse): void {
  form.enabled = cfg.enabled
  form.provider = cfg.provider
  form.model = cfg.model ?? ''
  form.base_url = cfg.base_url ?? ''
  form.api_key = ''
  form.clear_api_key = false
  form.max_auto_egress_level = cfg.max_auto_egress_level
  form.enable_inference = cfg.enable_inference
  form.enable_auto_translation = cfg.enable_auto_translation
  apiKeySource.value = cfg.api_key_source
  hasStoredKey.value = cfg.has_stored_api_key
}

watch(
  () => query.data.value,
  (cfg) => {
    if (cfg) hydrate(cfg)
  },
  { immediate: true },
)

const selectedProviderImpl = computed(
  () => PROVIDERS.find((p) => p.id === form.provider)?.implemented ?? false,
)
// off provider 下其余字段禁用(PRD §9 状态)。
const fieldsDisabled = computed(() => form.provider === 'off')

// 输入新 key 时,与"清除"互斥。
function onApiKeyInput(): void {
  if (form.api_key) form.clear_api_key = false
}
function toggleClearKey(): void {
  form.clear_api_key = !form.clear_api_key
  if (form.clear_api_key) form.api_key = ''
}

// ─── 保存 ────────────────────────────────────────────────────
const saveError = ref<string | null>(null)
const saveOk = ref(false)

// 前端先拦:选了未实现 provider 又要 enabled → 后端必 400,提前禁保存并提示。
const blockedByUnsupported = computed(() => form.enabled && !selectedProviderImpl.value)

const saveMutation = useMutation({
  mutationFn: () =>
    putAdminAiConfig({
      enabled: form.enabled,
      provider: form.provider,
      model: form.model.trim() || null,
      base_url: form.base_url.trim() || null,
      api_key: form.api_key ? form.api_key : null,
      clear_api_key: form.clear_api_key,
      max_auto_egress_level: form.max_auto_egress_level,
      l4_requires_optin: true, // 后端恒 true;L4 永远走显式 opt-in,前端不开放
      enable_inference: form.enable_inference,
      enable_auto_translation: form.enable_auto_translation,
    }),
  onSuccess: async (cfg) => {
    saveOk.value = true
    hydrate(cfg)
    await qc.invalidateQueries({ queryKey: ['admin-ai-config'] })
    setTimeout(() => (saveOk.value = false), 3000)
  },
})

async function onSave(): Promise<void> {
  saveError.value = null
  saveOk.value = false
  if (blockedByUnsupported.value) {
    saveError.value = t('ai.err_unsupported_provider')
    return
  }
  if (form.enabled && form.provider === 'openai_compatible' && !form.base_url.trim()) {
    saveError.value = t('ai.err_base_url_required')
    return
  }
  try {
    await saveMutation.mutateAsync()
  } catch (e) {
    saveError.value = errorMessage(e)
  }
}

// ─── 测试连接 ─────────────────────────────────────────────────
const testResult = ref<AdminAiConfigTestResponse | null>(null)
const testMutation = useMutation({
  mutationFn: testAdminAiConfig,
  onSuccess: (res) => {
    testResult.value = res
  },
})
async function onTest(): Promise<void> {
  testResult.value = null
  saveError.value = null
  if (blockedByUnsupported.value) {
    testResult.value = {
      ok: false,
      provider: form.provider,
      model: form.model || null,
      latency_ms: 0,
      error: 'unsupported_provider',
    }
    return
  }
  if (form.enabled && form.provider === 'openai_compatible' && !form.base_url.trim()) {
    testResult.value = {
      ok: false,
      provider: form.provider,
      model: form.model || null,
      latency_ms: 0,
      error: 'missing_provider_config',
    }
    return
  }
  try {
    await saveMutation.mutateAsync()
    await testMutation.mutateAsync()
  } catch (e) {
    // 非结构化错误(如 403):合成一个失败结果展示。
    testResult.value = {
      ok: false,
      provider: form.provider,
      model: form.model || null,
      latency_ms: 0,
      error: e instanceof ApiError ? e.code ?? 'unknown' : 'unknown',
    }
  }
}

// 把后端结构化 error 映射到 i18n;已知三类 + 其余回落通用。
function testErrorText(error: string | null): string {
  if (!error) return ''
  const known: Record<string, string> = {
    ai_disabled: t('ai.err_ai_disabled'),
    unsupported_provider: t('ai.err_unsupported_provider'),
    missing_provider_config: t('ai.err_missing_provider_config'),
  }
  return known[error] ?? t('ai.err_generic', { code: error })
}

</script>

<template>
  <div class="max-w-2xl mx-auto px-6 lg:px-10 py-8 w-full">
    <div class="mb-6">
      <h1 class="text-h2 font-semibold tracking-tight chrome-text-heading">{{ t('ai.title') }}</h1>
      <div class="text-sm chrome-text-muted mt-1">{{ t('ai.subtitle') }}</div>
    </div>

    <div v-if="query.isLoading.value" class="flex items-center justify-center gap-2 py-12 text-sm chrome-text-muted">
      <LoadingDots /><span>{{ t('common.loading') }}</span>
    </div>

    <div v-else-if="query.isError.value" class="border border-red-200 dark:border-red-500/30 bg-red-50 dark:bg-red-500/10 rounded-card p-5 flex items-start gap-3">
      <AlertTriangle class="w-5 h-5 text-red-500 shrink-0 mt-0.5" />
      <div>
        <div class="text-sm font-medium text-red-700 dark:text-red-400">{{ t('common.error') }}</div>
        <div class="text-sm text-red-600 dark:text-red-300 mt-0.5">{{ errorMessage(query.error.value) }}</div>
        <button @click="query.refetch()" type="button" class="text-xs text-red-700 dark:text-red-400 underline mt-2">{{ t('common.retry') }}</button>
      </div>
    </div>

    <div v-else class="space-y-5">
      <!-- ── provider 配置卡 ── -->
      <section class="chrome-bg-panel border chrome-border rounded-card p-5" style="box-shadow: var(--shadow-card);">
        <div class="flex items-center gap-2 mb-4">
          <Bot class="w-4 h-4 chrome-accent" />
          <h2 class="text-section font-semibold chrome-text-heading">{{ t('ai.provider_title') }}</h2>
        </div>

        <div class="space-y-4">
          <!-- enabled -->
          <label class="flex items-center justify-between gap-4 cursor-pointer">
            <div>
              <div class="text-sm font-medium chrome-text-normal">{{ t('ai.enabled') }}</div>
              <div class="text-xs chrome-text-muted">{{ t('ai.enabled_hint') }}</div>
            </div>
            <input v-model="form.enabled" type="checkbox" class="chrome-checkbox" data-testid="ai-enabled" />
          </label>

          <!-- provider -->
          <div class="space-y-1.5">
            <label class="form-label">{{ t('ai.provider') }}</label>
            <select v-model="form.provider" class="chrome-input w-full" data-testid="ai-provider">
              <option v-for="p in PROVIDERS" :key="p.id" :value="p.id">
                {{ p.id }}{{ p.implemented ? '' : ` — ${t('ai.provider_unimplemented')}` }}
              </option>
            </select>
            <p
              v-if="blockedByUnsupported"
              class="text-[11px] text-amber-600 dark:text-amber-400 inline-flex items-center gap-1"
              data-testid="ai-provider-warning"
            >
              <AlertTriangle class="w-3 h-3" /> {{ t('ai.provider_unimplemented_hint') }}
            </p>
          </div>

          <!-- model -->
          <div class="space-y-1.5">
            <label class="form-label">{{ t('ai.model') }}</label>
            <input v-model="form.model" type="text" maxlength="128" class="chrome-input w-full" :placeholder="t('ai.model_ph')" :disabled="fieldsDisabled" />
          </div>

          <!-- base_url -->
          <div class="space-y-1.5">
            <label class="form-label">{{ t('ai.base_url') }}</label>
            <input v-model="form.base_url" type="text" class="chrome-input w-full" :placeholder="t('ai.base_url_ph')" :disabled="fieldsDisabled" />
          </div>

          <!-- API key -->
          <div class="space-y-1.5">
            <label class="form-label">{{ t('ai.api_key') }}</label>
            <!-- 已存储态:展示来源 + 清除按钮 -->
            <div
              v-if="hasStoredKey && !form.clear_api_key"
              class="flex items-center justify-between gap-2 rounded-input chrome-bg-elevated px-3 py-2"
              data-testid="ai-key-stored"
            >
              <span class="text-xs chrome-text-normal inline-flex items-center gap-1.5">
                <KeyRound class="w-3.5 h-3.5 chrome-accent" />
                {{ t('ai.key_stored', { source: apiKeySource }) }}
              </span>
              <button type="button" class="inline-flex items-center gap-1.5 px-2 h-7 rounded-md whitespace-nowrap chrome-text-muted hover:chrome-bg-elevated hover:!text-red-500 transition-colors text-xs disabled:opacity-50 disabled:cursor-not-allowed" :disabled="fieldsDisabled" data-testid="ai-key-clear" @click="toggleClearKey">
                <Trash2 class="w-3.5 h-3.5" /> {{ t('ai.key_clear') }}
              </button>
            </div>
            <!-- env fallback 提示(无 stored,但环境变量里有)-->
            <div
              v-else-if="apiKeySource === 'env' && !form.clear_api_key"
              class="rounded-input chrome-bg-elevated px-3 py-2 text-xs chrome-text-muted"
              data-testid="ai-key-env"
            >
              {{ t('ai.key_env') }}
            </div>
            <!-- 清除待生效提示 -->
            <div
              v-if="form.clear_api_key"
              class="flex items-center justify-between gap-2 rounded-input px-3 py-2 text-xs"
              style="background-color: rgb(239 68 68 / 0.08); color: rgb(185 28 28);"
              data-testid="ai-key-clearing"
            >
              <span>{{ t('ai.key_clearing') }}</span>
              <button type="button" class="underline" @click="toggleClearKey">{{ t('common.cancel') }}</button>
            </div>
            <!-- 新 key 输入(password;与清除互斥)-->
            <input
              v-if="!form.clear_api_key"
              v-model="form.api_key"
              type="password"
              class="chrome-input w-full"
              :placeholder="hasStoredKey ? t('ai.api_key_replace_ph') : t('ai.api_key_ph')"
              autocomplete="off"
              :disabled="fieldsDisabled"
              data-testid="ai-key-input"
              @input="onApiKeyInput"
            />
            <p class="text-[11px] chrome-text-muted">{{ t('ai.api_key_hint') }}</p>
          </div>

          <!-- 功能开关 -->
          <div class="space-y-2.5 pt-1 border-t chrome-border-subtle">
            <label class="flex items-center justify-between gap-4 cursor-pointer pt-3">
              <span class="text-sm chrome-text-normal">{{ t('ai.enable_inference') }}</span>
              <input v-model="form.enable_inference" type="checkbox" class="chrome-checkbox" :disabled="fieldsDisabled" data-testid="ai-enable-inference" />
            </label>
            <label class="flex items-center justify-between gap-4 cursor-pointer">
              <span class="text-sm chrome-text-normal">{{ t('ai.enable_auto_translation') }}</span>
              <input v-model="form.enable_auto_translation" type="checkbox" class="chrome-checkbox" :disabled="fieldsDisabled" data-testid="ai-enable-translation" />
            </label>
          </div>
        </div>
      </section>

      <!-- ── 数据出站策略卡(L0–L3 + L4 锁定)── -->
      <section class="chrome-bg-panel border chrome-border rounded-card p-5" style="box-shadow: var(--shadow-card);">
        <div class="flex items-center gap-2 mb-1">
          <Lock class="w-4 h-4 chrome-accent" />
          <h2 class="text-section font-semibold chrome-text-heading">{{ t('ai.egress_title') }}</h2>
        </div>
        <p class="text-xs chrome-text-muted mb-4">{{ t('ai.egress_hint') }}</p>

        <div class="space-y-2" role="radiogroup">
          <label
            v-for="lv in EGRESS_LEVELS"
            :key="lv.level"
            class="flex items-center gap-3 rounded-input px-3 py-2.5 cursor-pointer border transition-colors"
            :class="form.max_auto_egress_level === lv.level
              ? 'chrome-border chrome-bg-elevated'
              : 'border-transparent hover:chrome-bg-elevated'"
            :data-testid="`ai-egress-l${lv.level}`"
          >
            <input
              v-model.number="form.max_auto_egress_level"
              type="radio"
              :value="lv.level"
              name="egress"
              class="chrome-radio"
            />
            <span class="w-2 h-2 rounded-full shrink-0" :class="lv.dot" />
            <div class="flex-1">
              <div class="text-sm font-medium chrome-text-normal">{{ t(`ai.egress_l${lv.level}_label`) }}</div>
              <div class="text-xs chrome-text-muted">{{ t(`ai.egress_l${lv.level}_desc`) }}</div>
            </div>
          </label>

          <!-- L4 永久锁定灰显 -->
          <div
            class="flex items-center gap-3 rounded-input px-3 py-2.5 border border-transparent opacity-55 cursor-not-allowed"
            :title="t('ai.egress_l4_locked_tooltip')"
            data-testid="ai-egress-l4-locked"
          >
            <Lock class="w-3.5 h-3.5 text-red-500 shrink-0" />
            <span class="w-2 h-2 rounded-full shrink-0 bg-red-500" />
            <div class="flex-1">
              <div class="text-sm font-medium chrome-text-normal">{{ t('ai.egress_l4_label') }}</div>
              <div class="text-xs chrome-text-muted">{{ t('ai.egress_l4_desc') }}</div>
            </div>
            <span class="text-[10px] uppercase tracking-wider text-red-500 border border-red-300 dark:border-red-500/40 rounded px-1.5 py-0.5">
              {{ t('ai.egress_l4_locked') }}
            </span>
          </div>
        </div>
      </section>

      <!-- ── 操作条:保存 + 测试连接 ── -->
      <section class="chrome-bg-panel border chrome-border rounded-card p-5" style="box-shadow: var(--shadow-card);">
        <div class="flex items-center justify-between gap-3 flex-wrap">
          <div class="flex items-center gap-2">
            <button type="button" class="chrome-btn-secondary" :disabled="testMutation.isPending.value" data-testid="ai-test-btn" @click="onTest">
              <template v-if="testMutation.isPending.value"><LoadingDots /><span>{{ t('ai.testing') }}</span></template>
              <template v-else><Plug class="w-4 h-4" /><span>{{ t('ai.test') }}</span></template>
            </button>
            <button type="button" class="chrome-btn-primary" :disabled="saveMutation.isPending.value" data-testid="ai-save-btn" @click="onSave">
              <template v-if="saveMutation.isPending.value"><LoadingDots /><span>{{ t('common.submitting') }}</span></template>
              <span v-else>{{ t('common.save') }}</span>
            </button>
          </div>
          <div v-if="saveOk" class="text-xs text-emerald-600 dark:text-emerald-400 inline-flex items-center gap-1">
            <CheckCircle2 class="w-3.5 h-3.5" /> {{ t('ai.saved') }}
          </div>
        </div>

        <div v-if="saveError" class="text-xs text-red-500 mt-3">{{ saveError }}</div>

        <!-- 测试结果 -->
        <div v-if="testResult" class="mt-4 pt-4 border-t chrome-border-subtle" data-testid="ai-test-result">
          <div
            v-if="testResult.ok"
            class="rounded-input border border-emerald-200 dark:border-emerald-500/30 bg-emerald-50 dark:bg-emerald-500/10 px-3 py-2.5 text-sm text-emerald-700 dark:text-emerald-300 flex items-center gap-2"
          >
            <CheckCircle2 class="w-4 h-4 shrink-0" />
            <span>{{ t('ai.test_ok', { latency: testResult.latency_ms }) }}</span>
            <span v-if="testResult.model" class="font-mono text-xs opacity-80">{{ testResult.model }}</span>
          </div>
          <div
            v-else
            class="rounded-input border border-red-200 dark:border-red-500/30 bg-red-50 dark:bg-red-500/10 px-3 py-2.5 text-sm text-red-700 dark:text-red-300 flex items-start gap-2"
          >
            <AlertTriangle class="w-4 h-4 shrink-0 mt-0.5" />
            <span>{{ testErrorText(testResult.error) }}</span>
          </div>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.form-label {
  @apply block text-xs uppercase tracking-wider font-medium;
  color: rgb(var(--text-muted));
}
.chrome-checkbox,
.chrome-radio {
  @apply w-4 h-4 rounded accent-current shrink-0;
  color: rgb(var(--accent));
}
</style>
