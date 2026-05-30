<script setup lang="ts">
import { onMounted, onUnmounted } from 'vue'
import { X } from 'lucide-vue-next'

const props = defineProps<{
  open: boolean
  title?: string
  subtitle?: string
}>()

const emit = defineEmits<{
  (e: 'close'): void
}>()

function onEsc(event: KeyboardEvent): void {
  if (event.key === 'Escape' && props.open) {
    emit('close')
  }
}

onMounted(() => window.addEventListener('keydown', onEsc))
onUnmounted(() => window.removeEventListener('keydown', onEsc))
</script>

<template>
  <Transition
    enter-active-class="transition duration-150 ease-out"
    enter-from-class="opacity-0"
    enter-to-class="opacity-100"
    leave-active-class="transition duration-100 ease-in"
    leave-from-class="opacity-100"
    leave-to-class="opacity-0"
  >
    <div
      v-if="open"
      class="fixed inset-0 z-50 backdrop-blur-sm flex items-center justify-center px-4 py-8"
      style="background-color: rgb(0 0 0 / 0.55);"
      @click.self="$emit('close')"
    >
      <Transition
        enter-active-class="transition duration-150 ease-out"
        enter-from-class="opacity-0 scale-95"
        enter-to-class="opacity-100 scale-100"
      >
        <div
          class="w-full max-w-md chrome-bg-panel border chrome-border rounded-card shadow-card max-h-[calc(100vh-4rem)] overflow-y-auto"
        >
          <div
            v-if="title || subtitle"
            class="flex items-start justify-between px-6 pt-6 pb-4 border-b chrome-border-subtle"
          >
            <div>
              <div
                v-if="title"
                class="text-section font-semibold tracking-tight chrome-text-heading"
              >
                {{ title }}
              </div>
              <div v-if="subtitle" class="text-sm chrome-text-muted mt-1">
                {{ subtitle }}
              </div>
            </div>
            <button
              type="button"
              @click="$emit('close')"
              class="chrome-btn-ghost -mr-1 -mt-1"
              aria-label="close"
            >
              <X class="w-4 h-4" />
            </button>
          </div>

          <div class="px-6 py-5">
            <slot />
          </div>
        </div>
      </Transition>
    </div>
  </Transition>
</template>
