/**
 * 4 个 design system 变体 —— 完整 token 集合,mode 已锁进 variant。
 *
 * 设计源:Figma Make 文件 ThemeContext.tsx(用户已锁定 4 选项)。
 * 默认 figma-dark(DBA 主战场最合身)。
 */
export type VariantId = 'spotify-dark' | 'figma-dark' | 'modern-light' | 'studio-light'

export interface Variant {
  id: VariantId
  name: string
  /** mode = dark | light(锁在 variant 里,不再单独 toggle)*/
  mode: 'dark' | 'light'
  /** 小色点指示色(Tailwind class)*/
  badge: string
  /** accent hex(SVG / 渐变里需要原始颜色字符串)*/
  accentHex: string
  hint: string
}

export const variants: Variant[] = [
  {
    id: 'spotify-dark',
    name: 'Spotify Dark',
    mode: 'dark',
    badge: 'bg-emerald-500',
    accentHex: '#10b981',
    hint: 'zinc + emerald,音乐 app 风',
  },
  {
    id: 'figma-dark',
    name: 'Figma Dark',
    mode: 'dark',
    badge: 'bg-sky-500',
    accentHex: '#0ea5e9',
    hint: 'slate + sky,设计工具风(推荐 DBA 主战场)',
  },
  {
    id: 'modern-light',
    name: 'Modern Light',
    mode: 'light',
    badge: 'bg-cyan-500',
    accentHex: '#06b6d4',
    hint: 'sky 浅底 + cyan,现代清爽',
  },
  {
    id: 'studio-light',
    name: 'Studio Light',
    mode: 'light',
    badge: 'bg-violet-500',
    accentHex: '#8b5cf6',
    hint: 'violet 浅底 + violet,品牌识别强',
  },
]

export const DEFAULT_VARIANT: VariantId = 'figma-dark'
