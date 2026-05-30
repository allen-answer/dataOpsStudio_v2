<script setup lang="ts">
import { storeToRefs } from 'pinia'
import { useThemeStore } from './stores/theme'

const themeStore = useThemeStore()
const { theme } = storeToRefs(themeStore)
</script>

<template>
  <div data-variant="default" :data-theme="theme" class="min-h-screen">
    <RouterView />
  </div>
</template>
