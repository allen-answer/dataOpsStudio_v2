<script setup lang="ts">
// 简单 3 点闪烁,工作区 / 提交按钮里都用
</script>

<template>
  <span class="inline-flex items-center gap-1" aria-label="loading">
    <span class="w-1 h-1 rounded-full bg-current opacity-60 animate-bounce" style="animation-delay: 0ms"></span>
    <span class="w-1 h-1 rounded-full bg-current opacity-60 animate-bounce" style="animation-delay: 150ms"></span>
    <span class="w-1 h-1 rounded-full bg-current opacity-60 animate-bounce" style="animation-delay: 300ms"></span>
  </span>
</template>
