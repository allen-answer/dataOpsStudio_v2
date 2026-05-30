import type { Config } from 'tailwindcss'

/**
 * Tailwind config — §10 design tokens 直接落 theme.extend。
 * sky.50 / sky.500 / sky.600 / slate.50 / slate.200 / slate.500 / slate.800 / slate.900
 * 都是 Tailwind 默认值且与契约 §10.2 数值一致,故直接复用,不在此重复定义。
 */
export default {
  content: ['./index.html', './src/**/*.{vue,ts}'],
  darkMode: ['selector', '[data-theme="dark"]'],
  theme: {
    extend: {
      fontFamily: {
        sans: [
          '-apple-system',
          'BlinkMacSystemFont',
          'Segoe UI',
          'Roboto',
          'Helvetica Neue',
          'Arial',
          'system-ui',
          'sans-serif',
        ],
        mono: [
          'JetBrains Mono',
          'Cascadia Code',
          'SF Mono',
          'Menlo',
          'ui-monospace',
          'monospace',
        ],
      },
      borderRadius: {
        // §10.2 形态
        tag: '4px',
        input: '6px',
        card: '8px', // 默认 8px,卡片/按钮一并用
      },
      boxShadow: {
        // §10.2 subtle —— 克制,不要重投影
        subtle: '0 1px 3px rgba(0, 0, 0, 0.08)',
      },
      fontSize: {
        // §10.2 字号:界面 14px / 数据 13px / 标题 16-20px
        ui: ['14px', { lineHeight: '1.5' }],
        data: ['13px', { lineHeight: '1.45' }],
        section: ['16px', { lineHeight: '1.4', letterSpacing: '-0.01em' }],
        h2: ['20px', { lineHeight: '1.3', letterSpacing: '-0.015em' }],
      },
      keyframes: {
        // running 状态用的脉冲(§10.3 状态色)—— 比 Tailwind 默认 pulse 慢且更柔
        'pulse-soft': {
          '0%, 100%': { opacity: '1', transform: 'scale(1)' },
          '50%': { opacity: '0.55', transform: 'scale(0.92)' },
        },
        'pulse-ring': {
          '0%': { transform: 'scale(0.9)', opacity: '0.55' },
          '70%, 100%': { transform: 'scale(1.7)', opacity: '0' },
        },
      },
      animation: {
        'pulse-soft': 'pulse-soft 1.8s ease-in-out infinite',
        'pulse-ring': 'pulse-ring 1.8s ease-out infinite',
      },
      backgroundImage: {
        // §10.4 signature 渐变 sky-400 → sky-600
        'sky-gradient':
          'linear-gradient(135deg, #38BDF8 0%, #0284C7 100%)',
        'sky-gradient-soft':
          'linear-gradient(135deg, #F0F9FF 0%, #E0F2FE 100%)',
      },
    },
  },
  plugins: [],
} satisfies Config
