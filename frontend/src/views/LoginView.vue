<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { Sparkles } from 'lucide-vue-next'
import { useAuthStore } from '../stores/auth'
import { ApiError } from '../api/types'
import LoadingDots from '../components/LoadingDots.vue'

const { t } = useI18n()
const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

const username = ref('')
const password = ref('')
const submitting = ref(false)
const errorMessage = ref<string | null>(null)
const usernameRef = ref<HTMLInputElement | null>(null)

const sessionExpired = computed(() => route.query.reason === 'expired')
const nextPath = computed(() =>
  typeof route.query.next === 'string' ? route.query.next : null,
)

onMounted(() => {
  usernameRef.value?.focus()
})

async function onSubmit(): Promise<void> {
  errorMessage.value = null

  if (!username.value.trim()) {
    errorMessage.value = t('auth.error_empty_username')
    return
  }
  if (!password.value) {
    errorMessage.value = t('auth.error_empty_password')
    return
  }

  submitting.value = true
  try {
    await auth.login(username.value.trim(), password.value)
    await router.push(nextPath.value && nextPath.value.startsWith('/') ? nextPath.value : '/projects')
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) {
      errorMessage.value = t('auth.error_invalid_credentials')
    } else if (e instanceof ApiError && e.status === 0) {
      errorMessage.value = t('common.error_network')
    } else if (e instanceof ApiError) {
      errorMessage.value = e.message || t('common.error_unknown')
    } else {
      errorMessage.value = t('common.error_unknown')
    }
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="min-h-screen flex flex-col items-center justify-center px-6 py-12">
    <div class="w-full max-w-sm">
      <!-- Brand block -->
      <div class="flex flex-col items-center mb-8">
        <div class="w-12 h-12 rounded-card bg-sky-gradient grid place-items-center shadow-subtle mb-4">
          <Sparkles class="w-6 h-6 text-white" />
        </div>
        <h1 class="text-h2 font-semibold tracking-tight text-slate-800 dark:text-slate-100">
          {{ t('auth.login_title') }}
        </h1>
        <div class="text-sm text-slate-500 dark:text-slate-400 mt-1">{{ t('auth.login_subtitle') }}</div>
      </div>

      <!-- Form -->
      <form
        @submit.prevent="onSubmit"
        class="bg-white dark:bg-slate-800/60 border border-slate-200 dark:border-slate-700 rounded-card shadow-subtle p-7 space-y-4"
      >
        <div
          v-if="sessionExpired"
          class="-mx-1 px-3 py-2 rounded-input bg-amber-50 dark:bg-amber-500/10 border border-amber-200 dark:border-amber-500/30 text-amber-700 dark:text-amber-400 text-xs"
        >
          {{ t('common.error_unauthorized') }}
        </div>

        <div class="space-y-1.5">
          <label class="block text-xs uppercase tracking-wider text-slate-500 dark:text-slate-400 font-medium">
            {{ t('auth.username') }}
          </label>
          <input
            ref="usernameRef"
            v-model="username"
            type="text"
            class="input"
            autocomplete="username"
            :disabled="submitting"
          />
        </div>

        <div class="space-y-1.5">
          <label class="block text-xs uppercase tracking-wider text-slate-500 dark:text-slate-400 font-medium">
            {{ t('auth.password') }}
          </label>
          <input
            v-model="password"
            type="password"
            class="input"
            autocomplete="current-password"
            :disabled="submitting"
          />
        </div>

        <div v-if="errorMessage" class="text-xs text-red-500 dark:text-red-400" role="alert">
          {{ errorMessage }}
        </div>

        <button type="submit" class="btn-primary w-full justify-center" :disabled="submitting">
          <template v-if="submitting">
            <LoadingDots />
            <span>{{ t('auth.submitting') }}</span>
          </template>
          <span v-else>{{ t('auth.submit') }}</span>
        </button>
      </form>

      <div class="text-center text-xs text-slate-400 dark:text-slate-500 mt-6 font-mono">
        DataOps Studio · 2.0
      </div>
    </div>
  </div>
</template>

<style scoped>
.btn-primary {
  @apply inline-flex items-center justify-center gap-1.5 px-4 py-2.5 text-ui font-medium select-none whitespace-nowrap transition-all duration-150 text-white;
  border-radius: var(--radius-card);
  background-image: var(--gradient-primary);
  box-shadow: var(--shadow-card);
}
.btn-primary:hover:not(:disabled) {
  background-image: var(--gradient-primary-hover);
}
.btn-primary:disabled {
  @apply opacity-60 cursor-not-allowed;
}

.input {
  @apply w-full border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900/50 px-3 py-2 text-ui text-slate-800 dark:text-slate-100 placeholder:text-slate-400 dark:placeholder:text-slate-500 transition-colors;
  border-radius: var(--radius-input);
}
.input:focus {
  @apply outline-none border-sky-500 dark:border-sky-400 ring-2 ring-sky-100 dark:ring-sky-500/20;
}
.input:disabled {
  @apply bg-slate-50 dark:bg-slate-900/30 cursor-not-allowed;
}
</style>
