import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { login as loginApi, refreshTokenApi, getMe, logout as logoutApi } from '../api/auth'

// Sprint 5 X / H5: cookie-only auth store. Tokens 不再進 localStorage —
// 認證狀態由 backend 設的 httpOnly cookie 決定，前端只記住目前登入的
// User 物件（用 /api/auth/me 重新確認）。瀏覽器重啟後第一次讀 user 會
// 觸發 /me；若 cookie 失效就回登入頁。
export const useAuthStore = defineStore('auth', () => {
  const user = ref(null)
  const initialized = ref(false)

  const isAuthenticated = computed(() => !!user.value)
  // Tier hierarchy (high → low): owner > admin > developer ≈ user.
  // ``isAdmin`` is admin-OR-above (matches backend's ``require_admin``,
  // which accepts both 'admin' and 'owner'). Owner-only UI gates on
  // ``isOwner`` directly — e.g. revealing model endpoint URLs / raw
  // audit log fields, or the auth-provider config form.
  const isOwner = computed(() => user.value?.role === 'owner')
  const isAdmin = computed(() =>
    user.value?.role === 'admin' || user.value?.role === 'owner',
  )
  const isDeveloper = computed(() =>
    user.value?.role === 'developer'
    || user.value?.role === 'admin'
    || user.value?.role === 'owner',
  )

  async function login(username, password, extra = {}) {
    // 後端 set cookies；body 仍帶 token 是給 SDK 用的，SPA 不再儲存。
    await loginApi(username, password, extra)
    await fetchUser()
  }

  async function refreshToken() {
    // 後端從 anila_refresh_token cookie 取 token；不需傳 body。
    await refreshTokenApi()
  }

  async function fetchUser() {
    try {
      const { data } = await getMe()
      user.value = data
    } catch {
      user.value = null
    } finally {
      initialized.value = true
    }
  }

  async function logout() {
    try {
      await logoutApi()
    } catch {
      // 後端 logout 失敗也要清前端狀態，避免使用者卡在 ghost session。
    }
    user.value = null
  }

  // 在第一次取用 store 時嘗試載入 /me：cookie 還在 → 自動還原 user；
  // 不在 → user 為 null，路由守衛會把使用者送去 /login。
  fetchUser()

  return {
    user,
    initialized,
    isAuthenticated,
    isOwner,
    isAdmin,
    isDeveloper,
    login,
    refreshToken,
    fetchUser,
    logout,
  }
})
