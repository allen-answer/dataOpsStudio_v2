import type { Router } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { setUnauthenticatedHandler, setTokenProvider } from '../api/client'

/**
 * 把 auth store ↔ api client ↔ router 三方接起来:
 *   - api client 拿 token 来自 auth store(getToken)
 *   - 401 时清 store + 跳 /login
 *   - 路由守卫:无 token 跳 /login,有 token 访问 /login 跳 /projects
 */
export function installAuthGuard(router: Router): void {
  const auth = useAuthStore()

  setTokenProvider(() => auth.token)

  setUnauthenticatedHandler(() => {
    auth.logout()
    void router.push({ name: 'login', query: { reason: 'expired' } })
  })

  router.beforeEach((to) => {
    const isPublic = to.meta?.public === true
    if (!auth.isAuthenticated && !isPublic) {
      return { name: 'login', query: to.fullPath === '/' ? undefined : { next: to.fullPath } }
    }
    if (auth.isAuthenticated && to.name === 'login') {
      return { name: 'projects' }
    }
  })
}
