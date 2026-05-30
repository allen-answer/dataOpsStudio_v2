import { createI18n } from 'vue-i18n'
import zhCN from './zh-CN'
import en from './en'

type MessageSchema = typeof zhCN

const STORAGE_KEY = 'dataops-locale'

function detectLocale(): 'zh-CN' | 'en' {
  const saved = localStorage.getItem(STORAGE_KEY)
  if (saved === 'zh-CN' || saved === 'en') return saved
  const browser = navigator.language
  return browser.startsWith('zh') ? 'zh-CN' : browser.startsWith('en') ? 'en' : 'zh-CN'
}

export const i18n = createI18n<[MessageSchema], 'zh-CN' | 'en'>({
  legacy: false,
  locale: detectLocale(),
  fallbackLocale: 'zh-CN',
  messages: {
    'zh-CN': zhCN,
    en,
  },
})

export function setLocale(locale: 'zh-CN' | 'en'): void {
  i18n.global.locale.value = locale
  localStorage.setItem(STORAGE_KEY, locale)
}
