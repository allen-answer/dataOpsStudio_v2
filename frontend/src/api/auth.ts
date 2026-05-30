import { apiClient } from './client'
import type { TokenResponse } from './types'

export function loginRequest(username: string, password: string): Promise<TokenResponse> {
  return apiClient.post<TokenResponse>('/auth/login', { username, password })
}
