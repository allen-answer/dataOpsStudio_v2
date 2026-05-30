<script setup lang="ts">
import { computed } from 'vue'
import { Palette, LayoutTemplate } from 'lucide-vue-next'
import { variants, type VariantId } from '../variants'

const props = defineProps<{ modelValue: VariantId }>()
defineEmits<{ (e: 'update:modelValue', v: VariantId): void }>()

const current = computed(() => variants.find((v) => v.id === props.modelValue))
</script>

<template>
  <div
    class="sticky top-0 z-40 h-14 shrink-0 backdrop-blur-md flex items-center gap-3 px-4 border-b chrome-bg-panel chrome-border-subtle"
  >
    <!-- Brand -->
    <div class="flex items-center gap-2.5 mr-1">
      <Palette class="w-4 h-4 chrome-text-muted" />
      <div class="leading-none">
        <div class="chrome-text-heading font-semibold text-xs">Visual Showcase</div>
        <div class="chrome-text-muted text-[10px]">DataOps Studio 2.0</div>
      </div>
    </div>

    <div class="w-px h-6 chrome-bg-elevated mx-1"></div>

    <!-- Theme presets -->
    <div class="flex items-center gap-1 p-0.5 rounded-lg border chrome-bg-elevated chrome-border-subtle">
      <button
        v-for="v in variants"
        :key="v.id"
        type="button"
        @click="$emit('update:modelValue', v.id)"
        class="flex items-center gap-1.5 px-2.5 py-1.5 rounded-md text-xs font-medium transition-all"
        :class="
          modelValue === v.id
            ? 'chrome-bg-panel chrome-text-heading shadow-sm'
            : 'chrome-text-muted hover:chrome-text-normal'
        "
      >
        <span class="w-2 h-2 rounded-full" :class="v.badge"></span>
        {{ v.name }}
      </button>
    </div>

    <!-- Hint -->
    <div class="ml-auto flex items-center gap-1.5 text-xs chrome-text-muted hidden md:flex">
      <LayoutTemplate class="w-3.5 h-3.5" />
      {{ current?.hint }}
    </div>
  </div>
</template>
