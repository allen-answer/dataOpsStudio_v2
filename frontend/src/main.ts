import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { VueQueryPlugin } from '@tanstack/vue-query'
import App from './App.vue'
import { router } from './router'
import { installAuthGuard } from './router/guards'
import { i18n } from './i18n'
import './style.css'

const app = createApp(App)

// ★ 顺序敏感:
//   1) createPinia 必须先 use,否则下面 installAuthGuard 里 useAuthStore() 抛
//   2) router use 后,导航守卫才生效
//   3) installAuthGuard 接 store ↔ api ↔ router 三方
app.use(createPinia())
app.use(router)
installAuthGuard(router)
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
