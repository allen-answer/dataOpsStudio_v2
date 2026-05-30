import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
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
