<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { LogOut, User as UserIcon, Sun, Moon, Languages, ChevronDown } from 'lucide-vue-next'
import { useAuthStore } from '../stores/auth'
import { useThemeStore } from '../stores/theme'
import { setLocale } from '../i18n'

const { t, locale } = useI18n()
const router = useRouter()
const auth = useAuthStore()
const theme = useThemeStore()

const open = ref(false)
const menuRef = ref<HTMLElement | null>(null)

function close(): void {
  open.value = false
}
function toggle(): void {
  open.value = !open.value
}

function onLogout(): void {
  close()
  auth.logout()
  void router.push({ name: 'login' })
}

function switchLocale(): void {
  setLocale(locale.value === 'zh-CN' ? 'en' : 'zh-CN')
}

function onClickOutside(event: MouseEvent): void {
  if (menuRef.value && !menuRef.value.contains(event.target as Node)) {
    close()
  }
}

onMounted(() => document.addEventListener('mousedown', onClickOutside))
onUnmounted(() => document.removeEventListener('mousedown', onClickOutside))
</script>

<template>
  <div ref="menuRef" class="relative">
    <button
      type="button"
      @click="toggle"
      class="flex items-center gap-2 px-2 py-1.5 rounded-card hover:bg-slate-100 dark:hover:bg-slate-800/60 transition-colors"
    >
      <div class="w-7 h-7 rounded-full bg-sky-gradient grid place-items-center shadow-subtle">
        <UserIcon class="w-3.5 h-3.5 text-white" />
      </div>
      <span class="text-sm text-slate-700 dark:text-slate-200 hidden md:inline">
        {{ auth.user?.id?.slice(0, 8) ?? '—' }}
      </span>
      <ChevronDown class="w-3.5 h-3.5 text-slate-400 dark:text-slate-500" />
    </button>

    <div
      v-if="open"
      class="absolute right-0 mt-2 w-56 bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-card shadow-subtle py-1.5 z-50"
    >
      <div class="px-3 py-2 border-b border-slate-100 dark:border-slate-700">
        <div class="text-xs text-slate-500 dark:text-slate-400 font-mono">
          {{ auth.user?.id ?? '—' }}
        </div>
        <div class="text-xs text-slate-400 dark:text-slate-500 mt-0.5">
          role: {{ auth.user?.role ?? '—' }}
        </div>
      </div>

      <button
        type="button"
        @click="theme.toggle()"
        class="w-full text-left px-3 py-2 text-sm hover:bg-slate-50 dark:hover:bg-slate-700/60 text-slate-700 dark:text-slate-200 flex items-center gap-2 transition-colors"
      >
        <component :is="theme.theme === 'dark' ? Sun : Moon" class="w-4 h-4" />
        {{ theme.theme === 'dark' ? t('common.light_mode') : t('common.dark_mode') }}
      </button>

      <button
        type="button"
        @click="switchLocale"
        class="w-full text-left px-3 py-2 text-sm hover:bg-slate-50 dark:hover:bg-slate-700/60 text-slate-700 dark:text-slate-200 flex items-center gap-2 transition-colors"
      >
        <Languages class="w-4 h-4" />
        {{ locale === 'zh-CN' ? 'English' : '简体中文' }}
      </button>

      <div class="border-t border-slate-100 dark:border-slate-700 mt-1 pt-1">
        <button
          type="button"
          @click="onLogout"
          class="w-full text-left px-3 py-2 text-sm hover:bg-red-50 dark:hover:bg-red-500/10 text-red-600 dark:text-red-400 flex items-center gap-2 transition-colors"
        >
          <LogOut class="w-4 h-4" />
          {{ t('common.logout') }}
        </button>
      </div>
    </div>
  </div>
</template>
