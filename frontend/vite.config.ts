import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  build: {
    // Monaco 编辑器 lazy 加载,自己单独占 ~600 KB gz —— 业务上算正常,
    // 不让 chunk size 警告刷屏。
    chunkSizeWarningLimit: 2400,
    rollupOptions: {
      output: {
        manualChunks(id: string) {
          // monaco 单独成 chunk,跟 SqlWorkspaceView 业务代码分开,
          // 缓存命中率更高(编辑业务改不影响 monaco chunk hash)。
          if (id.includes('node_modules/monaco-editor')) return 'monaco'
          // @antv/g6 血缘图引擎(懒加载),同理单独成 chunk 不进主包,
          // 只有打开血缘图才下载。
          if (id.includes('node_modules/@antv/')) return 'g6-vendor'
        },
      },
    },
  },
  server: {
    port: 5173,
    host: '127.0.0.1',
    /**
     * Dev proxy: /api/* → 后端 API
     *
     * 默认指向 localhost:8020,搭配 SSH 隧道使用:
     *   ssh -L 8020:127.0.0.1:8020 <deploy-host>
     * 隧道一开,Vite 自动把前端的 /api 请求打到云服务器后端。
     *
     * 想换端口/目标:export VITE_API_PROXY_TARGET=http://127.0.0.1:8001
     */
    proxy: {
      '/api': {
        target: process.env.VITE_API_PROXY_TARGET || 'http://127.0.0.1:8020',
        changeOrigin: true,
      },
    },
  },
})
