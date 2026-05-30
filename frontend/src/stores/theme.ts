/**
 * Variant store —— 4 个完整 token 主题切换(mode 已锁进 variant)。
 *
 * 与 Phase 1 不同:之前是 1 variant + 2 mode 双轴;现在是 4 variant 单轴,
 * 直接对齐 Figma Make 设计稿的 ThemeContext。
 */
import { defineStore } from 'pinia'
import { ref, watch } from 'vue'
import { DEFAULT_VARIANT, variants, type VariantId } from '../variants'

const STORAGE_KEY = 'dataops-variant'

const VALID_IDS = new Set<VariantId>(variants.map((v) => v.id))

function loadVariant(): VariantId {
  const saved = localStorage.getItem(STORAGE_KEY)
  if (saved && VALID_IDS.has(saved as VariantId)) return saved as VariantId
  return DEFAULT_VARIANT
}

export const useThemeStore = defineStore('theme', () => {
  const variant = ref<VariantId>(loadVariant())

  watch(variant, (v) => {
    localStorage.setItem(STORAGE_KEY, v)
  })

  function set(v: VariantId): void {
    variant.value = v
  }

  return { variant, set }
})

/** 兼容旧名 */
export type Theme = VariantId
