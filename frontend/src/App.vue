<script setup lang="ts">
import { ref, watch, onMounted } from 'vue'
import DesignSystemPreview from './views/DesignSystemPreview.vue'
import ThemeToggle, { type Theme } from './components/ThemeToggle.vue'

const THEME_KEY = 'dataops-theme'
const theme = ref<Theme>('light')

onMounted(() => {
  const saved = localStorage.getItem(THEME_KEY)
  if (saved === 'light' || saved === 'dark') {
    theme.value = saved
  }
})

watch(theme, (t) => {
  localStorage.setItem(THEME_KEY, t)
})
</script>

<template>
  <!--
    data-variant="default" 是当前定版的唯一一套 token,留住属性是为日后想加版
    直接在 style.css 加 [data-variant='xxx'] 块即可,不重构。
  -->
  <div data-variant="default" :data-theme="theme" class="min-h-screen">
    <!-- 极薄的 theme bar —— 设计系统预览页自用;真业务页面里 toggle 会去用户设置 -->
    <div
      class="sticky top-0 z-40 backdrop-blur-md bg-white/85 dark:bg-slate-900/85 border-b border-slate-200 dark:border-slate-800"
    >
      <div class="max-w-6xl mx-auto px-6 lg:px-10 py-2.5 flex items-center justify-end">
        <ThemeToggle v-model="theme" />
      </div>
    </div>
    <DesignSystemPreview />
  </div>
</template>
