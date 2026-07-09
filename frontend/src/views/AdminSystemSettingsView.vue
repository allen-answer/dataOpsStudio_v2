<script setup lang="ts">
import { computed, reactive, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useMutation, useQuery, useQueryClient } from '@tanstack/vue-query'
import { CheckCircle2, RefreshCw, Save, Settings, ShieldCheck, TimerReset } from 'lucide-vue-next'
import { getSystemSettings, putSystemSettings } from '../api/admin'
import { ApiError, type SystemSettingsUpdateRequest } from '../api/types'
import LoadingDots from '../components/LoadingDots.vue'

const { t } = useI18n()
const qc = useQueryClient()

const presets = [
  { label: '1h', value: 3600 },
  { label: '8h', value: 28800 },
  { label: '24h', value: 86400 },
  { label: '7d', value: 604800 },
]

const form = reactive<SystemSettingsUpdateRequest>({
  access_token_ttl_seconds: 3600,
  license_enforcement_enabled: true,
})
const saved = reactive({ ok: false, error: '' })

const query = useQuery({ queryKey: ['system-settings'], queryFn: getSystemSettings })

watch(
  () => query.data.value,
  (value) => {
    if (!value) return
    form.access_token_ttl_seconds = value.access_token_ttl_seconds
    form.license_enforcement_enabled = value.license_enforcement_enabled
  },
  { immediate: true },
)

const selectedPreset = computed(() =>
  presets.find((item) => item.value === form.access_token_ttl_seconds)?.value ?? 0,
)

const mutation = useMutation({
  mutationFn: () => putSystemSettings({ ...form }),
  onSuccess: async () => {
    saved.ok = true
    saved.error = ''
    await qc.invalidateQueries({ queryKey: ['system-settings'] })
    await qc.invalidateQueries({ queryKey: ['license-status'] })
    await qc.invalidateQueries({ queryKey: ['admin-license'] })
    setTimeout(() => (saved.ok = false), 3000)
  },
  onError: (error) => {
    saved.ok = false
    saved.error = error instanceof ApiError ? error.message : t('common.error_unknown')
  },
})

function setPreset(seconds: number): void {
  form.access_token_ttl_seconds = seconds
}

function submit(): void {
  saved.error = ''
  if (form.access_token_ttl_seconds < 300 || form.access_token_ttl_seconds > 2592000) {
    saved.error = t('admin.settings.ttl_range_error')
    return
  }
  void mutation.mutateAsync()
}

function reload(): void {
  void query.refetch()
}
</script>

<template>
  <div class="max-w-4xl mx-auto px-6 lg:px-10 py-8 w-full">
    <div class="mb-6 flex items-start justify-between gap-4">
      <div>
        <h1 class="text-h2 font-semibold tracking-tight chrome-text-heading">{{ t('admin.settings.title') }}</h1>
        <div class="text-sm chrome-text-muted mt-1">{{ t('admin.settings.subtitle') }}</div>
      </div>
      <button type="button" class="chrome-btn-secondary" @click="reload" :disabled="query.isFetching.value">
        <RefreshCw class="w-4 h-4" :class="query.isFetching.value ? 'animate-spin' : ''" />
        {{ t('common.refresh') }}
      </button>
    </div>

    <div v-if="query.isLoading.value" class="flex items-center justify-center gap-2 py-12 text-sm chrome-text-muted">
      <LoadingDots /><span>{{ t('common.loading') }}</span>
    </div>

    <div v-else class="space-y-5">
      <section class="chrome-bg-panel border chrome-border rounded-card p-5" style="box-shadow: var(--shadow-card);">
        <div class="flex items-center gap-2 mb-4">
          <TimerReset class="w-4 h-4 chrome-accent" />
          <h2 class="text-section font-semibold chrome-text-heading">{{ t('admin.settings.login_ttl_title') }}</h2>
        </div>
        <div class="grid gap-4 md:grid-cols-[220px_1fr] md:items-start">
          <label class="text-sm chrome-text-muted pt-2" for="login-ttl">
            {{ t('admin.settings.login_ttl_field') }}
          </label>
          <div class="space-y-3">
            <input
              id="login-ttl"
              v-model.number="form.access_token_ttl_seconds"
              type="number"
              min="300"
              max="2592000"
              step="60"
              class="chrome-input w-full md:w-64 tabular-nums"
            />
            <div class="flex flex-wrap gap-2">
              <button
                v-for="preset in presets"
                :key="preset.value"
                type="button"
                class="chrome-btn-secondary text-xs"
                :class="selectedPreset === preset.value ? 'ring-2 ring-sky-400/50' : ''"
                @click="setPreset(preset.value)"
              >
                {{ preset.label }}
              </button>
            </div>
            <p class="text-xs chrome-text-muted">{{ t('admin.settings.login_ttl_hint') }}</p>
          </div>
        </div>
      </section>

      <section class="chrome-bg-panel border chrome-border rounded-card p-5" style="box-shadow: var(--shadow-card);">
        <div class="flex items-center gap-2 mb-4">
          <ShieldCheck class="w-4 h-4 chrome-accent" />
          <h2 class="text-section font-semibold chrome-text-heading">{{ t('admin.settings.license_title') }}</h2>
        </div>
        <label class="flex items-center justify-between gap-4 cursor-pointer">
          <span>
            <span class="block text-sm font-medium chrome-text-heading">{{ t('admin.settings.license_toggle') }}</span>
            <span class="block text-xs chrome-text-muted mt-1">{{ t('admin.settings.license_hint') }}</span>
          </span>
          <input
            v-model="form.license_enforcement_enabled"
            type="checkbox"
            class="h-5 w-5 accent-sky-600"
          />
        </label>
      </section>

      <div v-if="saved.error" class="text-sm text-red-600 dark:text-red-400">{{ saved.error }}</div>
      <div v-if="saved.ok" class="text-sm text-emerald-600 dark:text-emerald-400 inline-flex items-center gap-1">
        <CheckCircle2 class="w-4 h-4" /> {{ t('admin.settings.save_ok') }}
      </div>

      <div class="flex justify-end">
        <button type="button" class="chrome-btn-primary" :disabled="mutation.isPending.value" @click="submit">
          <template v-if="mutation.isPending.value"><LoadingDots /><span>{{ t('common.submitting') }}</span></template>
          <template v-else><Save class="w-4 h-4" /><span>{{ t('common.save') }}</span></template>
        </button>
      </div>
    </div>
  </div>
</template>
