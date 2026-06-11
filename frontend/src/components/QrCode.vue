<script setup lang="ts">
/**
 * QrCode —— 把 otpauth:// URI 渲染成 QR 矢量图(inline SVG)。
 *
 * 选型(见 PR 描述):用 `qrcode`(MIT,事实标准,自带 TS 类型)的 `toString({type:'svg'})`,
 * 输出矢量 SVG —— 不依赖 <canvas>,任意缩放清晰,SSR / 无后端渲染测试里可直接断言节点存在。
 * 纯前端编码,otpauth URI 不出网(secret 只在本机生成 QR)。
 */
import { ref, watch } from 'vue'
import QRCode from 'qrcode'

const props = withDefaults(
  defineProps<{
    value: string
    size?: number
  }>(),
  { size: 176 },
)

const svg = ref<string>('')
const error = ref(false)

async function render(value: string): Promise<void> {
  error.value = false
  if (!value) {
    svg.value = ''
    return
  }
  try {
    // margin:1 = 留 1 模块静默区;errorCorrectionLevel M 足够 otpauth URI。
    svg.value = await QRCode.toString(value, {
      type: 'svg',
      margin: 1,
      errorCorrectionLevel: 'M',
    })
  } catch {
    error.value = true
    svg.value = ''
  }
}

watch(() => props.value, render, { immediate: true })
</script>

<template>
  <div
    class="inline-grid place-items-center rounded-card bg-white p-3 shadow-card"
    :style="{ width: `${size}px`, height: `${size}px` }"
    data-testid="mfa-qr"
  >
    <!-- qrcode 输出的是受信任的本地生成 SVG(无用户输入注入面),v-html 安全 -->
    <div v-if="svg" class="w-full h-full [&>svg]:w-full [&>svg]:h-full" v-html="svg" />
    <span v-else-if="error" class="text-xs text-red-500 px-2 text-center">QR</span>
  </div>
</template>
