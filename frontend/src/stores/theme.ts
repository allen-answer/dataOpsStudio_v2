import { defineStore } from 'pinia'
import { ref, watch } from 'vue'

export type Theme = 'light' | 'dark'

const STORAGE_KEY = 'dataops-theme'

function loadTheme(): Theme {
  const saved = localStorage.getItem(STORAGE_KEY)
  if (saved === 'light' || saved === 'dark') return saved
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

export const useThemeStore = defineStore('theme', () => {
  const theme = ref<Theme>(loadTheme())

  watch(theme, (t) => {
    localStorage.setItem(STORAGE_KEY, t)
  })

  function toggle(): void {
    theme.value = theme.value === 'light' ? 'dark' : 'light'
  }
  function set(t: Theme): void {
    theme.value = t
  }

  return { theme, toggle, set }
})
