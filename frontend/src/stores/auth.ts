import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { loginRequest } from '../api/auth'
import type { User } from '../api/types'

const TOKEN_KEY = 'dataops-token'

/**
 * 解 JWT payload(只取 sub / role,不验签 —— 验签是后端的事;
 * 拿过期的 token 调 API 反正会 401,触发 setUnauthenticatedHandler 清场).
 */
function decodeJwt(token: string): User | null {
  try {
    const parts = token.split('.')
    if (parts.length !== 3) return null
    const payload = JSON.parse(atob(parts[1].replace(/-/g, '+').replace(/_/g, '/')))
    if (typeof payload.sub !== 'string') return null
    return {
      id: payload.sub,
      role: typeof payload.role === 'string' ? payload.role : 'member',
    }
  } catch {
    return null
  }
}

export const useAuthStore = defineStore('auth', () => {
  const token = ref<string | null>(localStorage.getItem(TOKEN_KEY))
  const user = ref<User | null>(token.value ? decodeJwt(token.value) : null)

  const isAuthenticated = computed(() => token.value !== null && user.value !== null)

  async function login(username: string, password: string): Promise<void> {
    const response = await loginRequest(username, password)
    token.value = response.access_token
    user.value = decodeJwt(response.access_token)
    if (user.value === null) {
      logout()
      throw new Error('Server returned an unreadable token')
    }
    localStorage.setItem(TOKEN_KEY, response.access_token)
  }

  function logout(): void {
    token.value = null
    user.value = null
    localStorage.removeItem(TOKEN_KEY)
  }

  return { token, user, isAuthenticated, login, logout }
})
