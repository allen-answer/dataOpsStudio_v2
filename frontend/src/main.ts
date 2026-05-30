import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { VueQueryPlugin } from '@tanstack/vue-query'
import App from './App.vue'
import { router } from './router'
import { i18n } from './i18n'
import './style.css'

const app = createApp(App)

// 顺序:Pinia 必须先于 router/guards.ts,因为 installAuthGuard 用 useAuthStore.
app.use(createPinia())
app.use(router)
app.use(i18n)
app.use(VueQueryPlugin, {
  queryClientConfig: {
    defaultOptions: {
      queries: {
        staleTime: 30_000,
        refetchOnWindowFocus: false,
        retry: 1,
      },
    },
  },
})

app.mount('#app')
