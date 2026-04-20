import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { login as loginApi, refreshTokenApi, getMe } from '../api/auth'

export const useAuthStore = defineStore('auth', () => {
  const accessToken = ref(localStorage.getItem('accessToken') || '')
  const refreshTokenValue = ref(localStorage.getItem('refreshToken') || '')
  const user = ref(null)

  const isAuthenticated = computed(() => !!accessToken.value)
  const isAdmin = computed(() => user.value?.role === 'admin')
  const isDeveloper = computed(() => user.value?.role === 'developer' || user.value?.role === 'admin')

  async function login(username, password, extra = {}) {
    const { data } = await loginApi(username, password, extra)
    accessToken.value = data.access_token
    refreshTokenValue.value = data.refresh_token
    localStorage.setItem('accessToken', data.access_token)
    localStorage.setItem('refreshToken', data.refresh_token)
    await fetchUser()
  }

  async function refreshToken() {
    const { data } = await refreshTokenApi(refreshTokenValue.value)
    accessToken.value = data.access_token
    refreshTokenValue.value = data.refresh_token
    localStorage.setItem('accessToken', data.access_token)
    localStorage.setItem('refreshToken', data.refresh_token)
  }

  async function fetchUser() {
    try {
      const { data } = await getMe()
      user.value = data
    } catch {
      logout()
    }
  }

  function logout() {
    accessToken.value = ''
    refreshTokenValue.value = ''
    user.value = null
    localStorage.removeItem('accessToken')
    localStorage.removeItem('refreshToken')
  }

  // Auto-fetch user on init if token exists
  if (accessToken.value) {
    fetchUser()
  }

  return {
    accessToken,
    user,
    isAuthenticated,
    isAdmin,
    isDeveloper,
    login,
    refreshToken,
    fetchUser,
    logout,
  }
})
