/**
 * 认证状态。
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { api, auth } from '@/api/client'
import type { UserInfo } from '@/api/types'

export const useAuthStore = defineStore('auth', () => {
  const token = ref<string | null>(auth.token)
  const username = ref<string | null>(auth.user)
  const checked = ref(false)

  const isAuthenticated = computed(() => Boolean(token.value))

  async function login(user: string, password: string): Promise<void> {
    const res = await api.login(user, password)
    auth.save(res.access_token, res.username)
    token.value = res.access_token
    username.value = res.username
  }

  function logout(): void {
    auth.clear()
    token.value = null
    username.value = null
    checked.value = false
  }

  /** 校验本地令牌是否仍然有效。 */
  async function check(): Promise<boolean> {
    if (!token.value) {
      checked.value = true
      return false
    }
    try {
      const user: UserInfo = await api.me()
      username.value = user.username
      auth.save(token.value, user.username)
      checked.value = true
      return true
    } catch {
      logout()
      checked.value = true
      return false
    }
  }

  function setSession(newToken: string, newUsername: string): void {
    auth.save(newToken, newUsername)
    token.value = newToken
    username.value = newUsername
  }

  return { token, username, checked, isAuthenticated, login, logout, check, setSession }
})
