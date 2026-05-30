<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import { Hourglass } from 'lucide-vue-next'
import EmptyState from '../components/EmptyState.vue'

const props = defineProps<{ section: string }>()
const { t } = useI18n()
</script>

<template>
  <div class="max-w-5xl mx-auto px-6 lg:px-10 py-10">
    <div class="bg-white dark:bg-slate-800/60 border border-slate-200 dark:border-slate-700 rounded-card shadow-subtle">
      <EmptyState
        :icon="Hourglass"
        :title="t('placeholder.coming_soon')"
        :hint="t('placeholder.coming_soon_hint', { section: t(`nav.${props.section}`) })"
      />
    </div>
  </div>
</template>
