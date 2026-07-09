<script setup lang="ts">
/**
 * AdminLicenseView —— /admin/license(D.4 + License 管理)。
 *
 * 后端(app/api/routes/admin.py):
 *   GET /admin/license   → LicenseStatusResponse(含 limits)
 *   PUT /admin/license   → 上传 / 替换 license 文本({license_text}),签名验证后落库
 *
 * ★ PUT /admin/license 是 REPAIR / IN_GRACE 下唯一不被 middleware 403 的写操作
 *   (crosscutting.py _LICENSE_UPDATE_PATHS),所以本页"替换 license"按钮在任何
 *   license 模式下都可用。
 */
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useQuery, useMutation, useQueryClient } from '@tanstack/vue-query'
import { KeyRound, AlertTriangle, CheckCircle2, Clock, ShieldAlert, Upload, Clipboard, Download, FileText } from 'lucide-vue-next'
import { getAdminLicense, putAdminLicense } from '../api/admin'
import { ApiError, type LicenseStatus } from '../api/types'
import LoadingDots from '../components/LoadingDots.vue'

const { t } = useI18n()
const qc = useQueryClient()

const query = useQuery({ queryKey: ['admin-license'], queryFn: getAdminLicense })
const status = computed<LicenseStatus | null>(() => query.data.value ?? null)
const mode = computed(() => status.value?.mode ?? null)

// ─── 上传 / 替换 ──────────────────────────────────────────
const licenseText = ref('')
const uploadError = ref<string | null>(null)
const uploadOk = ref(false)
const templateOk = ref(false)

const LICENSE_TEMPLATE = JSON.stringify(
  {
    payload: {
      edition: 'enterprise',
      customer: 'example-customer',
      expires_at: '2027-12-31T23:59:59Z',
      features: ['sql_workspace', 'compare', 'lineage', 'workflow'],
      limits: {
        users: 20,
        datasources: 50,
      },
    },
    signature: '<signature>',
  },
  null,
  2,
)

const uploadMutation = useMutation({
  mutationFn: () => putAdminLicense(licenseText.value),
  onSuccess: async () => {
    uploadOk.value = true
    licenseText.value = ''
    await qc.invalidateQueries({ queryKey: ['admin-license'] })
    await qc.invalidateQueries({ queryKey: ['license-status'] })
    setTimeout(() => (uploadOk.value = false), 3000)
  },
})

async function onUpload(): Promise<void> {
  uploadError.value = null
  uploadOk.value = false
  if (!licenseText.value.trim()) {
    uploadError.value = t('admin.license.error_empty')
    return
  }
  try {
    await uploadMutation.mutateAsync()
  } catch (e) {
    uploadError.value = e instanceof ApiError ? e.message : t('common.error_unknown')
  }
}

async function onFile(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  licenseText.value = await file.text()
  input.value = ''
}

function fillTemplate(): void {
  licenseText.value = LICENSE_TEMPLATE
}

async function copyTemplate(): Promise<void> {
  await navigator.clipboard.writeText(LICENSE_TEMPLATE)
  templateOk.value = true
  setTimeout(() => (templateOk.value = false), 2500)
}

function downloadTemplate(): void {
  const blob = new Blob([LICENSE_TEMPLATE], { type: 'application/json;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = 'license-template.json'
  a.click()
  URL.revokeObjectURL(url)
}

// ─── 展示 ─────────────────────────────────────────────────
const MODE_META: Record<string, { icon: typeof CheckCircle2; class: string }> = {
  valid: { icon: CheckCircle2, class: 'text-emerald-600 dark:text-emerald-400' },
  trial: { icon: Clock, class: 'text-sky-600 dark:text-sky-400' },
  in_grace: { icon: AlertTriangle, class: 'text-amber-600 dark:text-amber-400' },
  expired: { icon: AlertTriangle, class: 'text-red-600 dark:text-red-400' },
  repair: { icon: ShieldAlert, class: 'text-red-600 dark:text-red-400' },
}
const modeMeta = computed(() => (mode.value ? MODE_META[mode.value] ?? MODE_META.valid : MODE_META.valid))

function formatDate(iso: string | null): string {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleString(undefined, {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    })
  } catch {
    return iso
  }
}

const limitEntries = computed(() => Object.entries(status.value?.limits ?? {}))

function errorMessage(): string {
  const e = query.error.value
  if (e instanceof ApiError) {
    if (e.status === 403) return t('admin.forbidden_hint')
    return e.message || t('common.error_unknown')
  }
  return t('common.error_unknown')
}
</script>

<template>
  <div class="max-w-3xl mx-auto px-6 lg:px-10 py-8 w-full">
    <div class="mb-6">
      <h1 class="text-h2 font-semibold tracking-tight chrome-text-heading">{{ t('admin.license.title') }}</h1>
      <div class="text-sm chrome-text-muted mt-1">{{ t('admin.license.subtitle') }}</div>
    </div>

    <div v-if="query.isLoading.value" class="flex items-center justify-center gap-2 py-12 text-sm chrome-text-muted">
      <LoadingDots /><span>{{ t('common.loading') }}</span>
    </div>

    <div v-else-if="query.isError.value" class="border border-red-200 dark:border-red-500/30 bg-red-50 dark:bg-red-500/10 rounded-card p-5 flex items-start gap-3">
      <AlertTriangle class="w-5 h-5 text-red-500 shrink-0 mt-0.5" />
      <div>
        <div class="text-sm font-medium text-red-700 dark:text-red-400">{{ t('common.error') }}</div>
        <div class="text-sm text-red-600 dark:text-red-300 mt-0.5">{{ errorMessage() }}</div>
        <button @click="query.refetch()" type="button" class="text-xs text-red-700 dark:text-red-400 underline mt-2">{{ t('common.retry') }}</button>
      </div>
    </div>

    <template v-else-if="status">
      <!-- 状态卡 -->
      <div class="chrome-bg-panel border chrome-border rounded-card p-5 mb-5" style="box-shadow: var(--shadow-card);">
        <div class="flex items-center gap-3 mb-4">
          <component :is="modeMeta.icon" class="w-6 h-6" :class="modeMeta.class" />
          <div>
            <div class="text-section font-semibold chrome-text-heading">
              {{ t(`license.mode_${mode}`) }}
            </div>
            <div v-if="status.trial_days_remaining != null" class="text-xs chrome-text-muted mt-0.5">
              {{ t('admin.license.days_remaining', { days: status.trial_days_remaining }) }}
            </div>
            <div v-if="status.repair_reason" class="text-xs text-red-600 dark:text-red-400 mt-0.5">
              {{ status.repair_reason }}
            </div>
          </div>
        </div>

        <div
          v-if="!status.license_enforcement_enabled"
          class="mb-4 border border-amber-200 dark:border-amber-500/30 bg-amber-50 dark:bg-amber-500/10 rounded-card px-3 py-2 text-sm text-amber-700 dark:text-amber-300"
        >
          {{ t('admin.license.enforcement_disabled') }}
        </div>

        <dl class="grid grid-cols-2 gap-x-6 gap-y-2.5 text-sm">
          <div class="flex justify-between gap-4 border-b chrome-border-subtle pb-2">
            <dt class="chrome-text-muted">{{ t('admin.license.edition') }}</dt>
            <dd class="chrome-text-normal font-medium">{{ status.edition ?? '—' }}</dd>
          </div>
          <div class="flex justify-between gap-4 border-b chrome-border-subtle pb-2">
            <dt class="chrome-text-muted">{{ t('admin.license.customer') }}</dt>
            <dd class="chrome-text-normal font-medium">{{ status.customer ?? '—' }}</dd>
          </div>
          <div class="flex justify-between gap-4 border-b chrome-border-subtle pb-2">
            <dt class="chrome-text-muted">{{ t('admin.license.expires_at') }}</dt>
            <dd class="chrome-text-normal font-medium tabular-nums">{{ formatDate(status.expires_at) }}</dd>
          </div>
          <div class="flex justify-between gap-4 border-b chrome-border-subtle pb-2">
            <dt class="chrome-text-muted">{{ t('admin.license.features') }}</dt>
            <dd class="chrome-text-normal font-medium">{{ status.features.length ? status.features.join(', ') : '—' }}</dd>
          </div>
        </dl>

        <!-- limits -->
        <div v-if="limitEntries.length" class="mt-4 pt-3 border-t chrome-border-subtle">
          <div class="text-xs uppercase tracking-wider chrome-text-muted font-medium mb-2">{{ t('admin.license.limits') }}</div>
          <div class="grid grid-cols-2 gap-x-6 gap-y-1.5 text-sm">
            <div v-for="[k, v] in limitEntries" :key="k" class="flex justify-between gap-4">
              <span class="chrome-text-muted font-mono text-xs">{{ k }}</span>
              <span class="chrome-text-normal font-mono text-xs">{{ String(v) }}</span>
            </div>
          </div>
        </div>
      </div>

      <!-- 上传 / 替换 -->
      <div class="chrome-bg-panel border chrome-border rounded-card p-5" style="box-shadow: var(--shadow-card);">
        <div class="flex items-center gap-2 mb-3">
          <Upload class="w-4 h-4 chrome-accent" />
          <h2 class="text-section font-semibold chrome-text-heading">{{ t('admin.license.upload_title') }}</h2>
        </div>
        <p class="text-sm chrome-text-muted mb-3">{{ t('admin.license.upload_hint') }}</p>

        <div class="flex flex-wrap gap-2 mb-3">
          <button type="button" class="chrome-btn-secondary" @click="fillTemplate">
            <FileText class="w-3.5 h-3.5" />
            {{ t('admin.license.template_fill') }}
          </button>
          <button type="button" class="chrome-btn-secondary" @click="copyTemplate">
            <Clipboard class="w-3.5 h-3.5" />
            {{ t('admin.license.template_copy') }}
          </button>
          <button type="button" class="chrome-btn-secondary" @click="downloadTemplate">
            <Download class="w-3.5 h-3.5" />
            {{ t('admin.license.template_download') }}
          </button>
          <span v-if="templateOk" class="text-xs text-emerald-600 dark:text-emerald-400 inline-flex items-center gap-1 px-1">
            <CheckCircle2 class="w-3.5 h-3.5" /> {{ t('admin.license.template_copied') }}
          </span>
        </div>

        <div class="space-y-3">
          <label class="chrome-btn-secondary cursor-pointer inline-flex">
            <Upload class="w-3.5 h-3.5" />
            {{ t('admin.license.choose_file') }}
            <input type="file" accept=".lic,.txt,.json,text/*" class="hidden" @change="onFile" />
          </label>
          <textarea
            v-model="licenseText"
            rows="6"
            class="chrome-input w-full font-mono text-xs resize-y"
            :placeholder="t('admin.license.paste_placeholder')"
            :disabled="uploadMutation.isPending.value"
          />
          <div v-if="uploadError" class="text-xs text-red-500">{{ uploadError }}</div>
          <div v-if="uploadOk" class="text-xs text-emerald-600 dark:text-emerald-400 inline-flex items-center gap-1">
            <CheckCircle2 class="w-3.5 h-3.5" /> {{ t('admin.license.upload_ok') }}
          </div>
          <div class="flex justify-end">
            <button type="button" class="chrome-btn-primary" :disabled="uploadMutation.isPending.value" @click="onUpload">
              <template v-if="uploadMutation.isPending.value"><LoadingDots /><span>{{ t('common.submitting') }}</span></template>
              <template v-else><KeyRound class="w-4 h-4" /><span>{{ t('admin.license.upload_submit') }}</span></template>
            </button>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>
