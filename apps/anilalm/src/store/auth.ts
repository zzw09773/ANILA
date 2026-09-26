import { create } from 'zustand'
import { bindAuthAdapter } from '../api/client'
import { getMe, logoutApi, refreshToken as refreshApi } from '../api/auth'
import type { UserMe } from '../types'
import { wipeAuthStorage } from './authStorage'

interface AuthState {
  user: UserMe | null
  // 'idle'     = 初始,尚未探測 session
  // 'checking' = 正在用 cookie 探測 /api/auth/me(跨 app SSO 接手)
  // 'authed'   = 已驗證
  // 'unauth'   = 已探測但未登入 → ProtectedRoute 會導去 CSP /login
  status: 'idle' | 'checking' | 'authed' | 'unauth'

  refresh: () => Promise<boolean>
  fetchMe: () => Promise<void>
  logout: () => void
  hydrate: () => Promise<void>
}

// 同時到期的請求只換發一次。第二次換發會把上一把 refresh cookie 作廢。
let refreshInflight: Promise<boolean> | null = null

// 清掉舊版寫進 localStorage 的權杖。工作階段只留在 httpOnly cookie。
if (typeof localStorage !== 'undefined') {
  wipeAuthStorage(localStorage)
}

export const useAuthStore = create<AuthState>()((set, get) => ({
  user: null,
  status: 'idle',

  refresh: () => {
    if (refreshInflight) return refreshInflight
    refreshInflight = (async () => {
      try {
        // 後端從 anila_refresh_token cookie 取權杖，並改寫 cookie。
        // 回應本文仍帶權杖給 SDK，這裡不讀、不留。
        await refreshApi()
        return true
      } catch {
        set({ user: null, status: 'unauth' })
        return false
      } finally {
        refreshInflight = null
      }
    })()
    return refreshInflight
  },

  fetchMe: async () => {
    try {
      const { data } = await getMe()
      set({ user: data, status: 'authed' })
    } catch {
      set({ user: null, status: 'unauth' })
    }
  },

  logout: () => {
    // 伺服器登出是盡力而為。本地狀態先清，網路失敗也算已登出。
    void logoutApi().catch(() => undefined)
    set({
      user: null,
      status: 'unauth',
    })
  },

  hydrate: async () => {
    // Cookie-first session pickup. CSP 登入設 httpOnly anila_access_token
    // cookie 在 path '/'(host-scoped,同源這個 SPA 也帶得到);getMe() 靠
    // client 的 withCredentials 把 cookie 送出。所以**無條件**探測 —
    // 即使本地沒有任何 JS-visible token(跨 app SSO:user 在 CSP 登入後直接
    // 進來這頁)。探測前先標 'checking',ProtectedRoute 會等結果再決定,
    // 不會在 cookie 還沒驗完就把人踢回 /login。
    set({ status: 'checking' })
    await get().fetchMe()
  },
}))

// 模組載入時就把 axios 接到這個 store。store 已同步建好，第一個請求會用得到。
bindAuthAdapter({
  refresh: () => useAuthStore.getState().refresh(),
  logout: () => useAuthStore.getState().logout(),
})
