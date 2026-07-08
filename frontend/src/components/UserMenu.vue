<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { LogOut, User as UserIcon, Palette, Languages, ChevronDown, ShieldCheck, KeyRound } from 'lucide-vue-next'
import { useAuthStore } from '../stores/auth'
import { useThemeStore } from '../stores/theme'
import { variants, type VariantId } from '../variants'
import { setLocale } from '../i18n'

const { t, locale } = useI18n()
const router = useRouter()
const auth = useAuthStore()
const themeStore = useThemeStore()

const isAdmin = computed(() => auth.user?.role === 'admin')
function goAdmin(): void {
  close()
  void router.push({ name: 'admin-users' })
}
function goAccountSecurity(): void {
  close()
  void router.push({ name: 'account-security' })
}

// 四个 variant 直接可选(不再循环切换);菜单保持打开方便对比预览。
function setVariant(id: VariantId): void {
  themeStore.set(id)
}

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

      <div class="px-3 py-2 flex items-center gap-2 chrome-text-normal">
        <Palette class="w-4 h-4 shrink-0" />
        <span class="flex-1 text-sm">{{ t('common.switch_theme') }}</span>
        <div class="flex items-center gap-1">
          <button
            v-for="v in variants"
            :key="v.id"
            type="button"
            @click="setVariant(v.id)"
            :title="v.name"
            :aria-label="v.name"
            :aria-pressed="themeStore.variant === v.id"
            class="w-5 h-5 rounded-full grid place-items-center transition-all"
            :class="
              themeStore.variant === v.id
                ? 'ring-2 ring-slate-400 dark:ring-slate-300'
                : 'opacity-70 hover:opacity-100'
            "
          >
            <span class="w-2.5 h-2.5 rounded-full" :class="v.badge"></span>
          </button>
        </div>
      </div>

      <button
        type="button"
        @click="switchLocale"
        class="w-full text-left px-3 py-2 text-sm hover:bg-slate-50 dark:hover:bg-slate-700/60 text-slate-700 dark:text-slate-200 flex items-center gap-2 transition-colors"
      >
        <Languages class="w-4 h-4" />
        {{ locale === 'zh-CN' ? 'English' : '简体中文' }}
      </button>

      <button
        type="button"
        @click="goAccountSecurity"
        class="w-full text-left px-3 py-2 text-sm hover:chrome-bg-elevated chrome-text-normal flex items-center gap-2 transition-colors"
      >
        <KeyRound class="w-4 h-4" />
        {{ t('account.menu_entry') }}
      </button>

      <button
        v-if="isAdmin"
        type="button"
        @click="goAdmin"
        class="w-full text-left px-3 py-2 text-sm hover:chrome-bg-elevated chrome-text-normal flex items-center gap-2 transition-colors"
      >
        <ShieldCheck class="w-4 h-4" />
        {{ t('admin.menu_entry') }}
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
