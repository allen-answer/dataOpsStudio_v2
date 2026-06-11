<script setup lang="ts">
import { storeToRefs } from 'pinia'
import { useThemeStore } from './stores/theme'
import VariantSwitcher from './components/VariantSwitcher.vue'
import ToastHost from './components/ToastHost.vue'

const themeStore = useThemeStore()
const { variant } = storeToRefs(themeStore)

function onUpdate(v: typeof variant.value): void {
  themeStore.set(v)
}
</script>

<template>
  <!--
    data-variant 锁 4 选一(mode 已锁进 variant),不再分离 light/dark toggle。
    body 颜色 / 全局 chrome 都跟着 data-variant 走(见 style.css)。

    VariantSwitcher 是 dev / design review 期间常驻顶条;production 阶段可移到
    用户设置或通过 env flag 隐藏。当前阶段我们就在 design review,留着方便切。
  -->
  <div :data-variant="variant" class="min-h-screen flex flex-col chrome-bg-main">
    <VariantSwitcher :model-value="variant" @update:model-value="onUpdate" />
    <div class="flex-1 min-h-0 flex">
      <RouterView />
    </div>
    <ToastHost />
  </div>
</template>
