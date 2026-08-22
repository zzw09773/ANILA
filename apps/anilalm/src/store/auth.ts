import { create } from 'zustand'
import { bindAuthAdapter, explainError } from '../api/client'
import { getMe, login as loginApi, logoutApi, refreshToken as refreshApi } from '../api/auth'
import type { UserMe } from '../types'
import { wipeAuthStorage } from './authStorage'

interface AuthState {
  accessToken: string | null
  refreshToken: string | null
  user: UserMe | null
  // 'idle'     = 初始,尚未探測 session
  // 'checking' = 正在用 cookie 探測 /api/auth/me(跨 app SSO 接手)
  // 'loading'  = 帳密登入進行中
  // 'authed'   = 已驗證
  // 'unauth'   = 已探測但未登入 → ProtectedRoute 會導去 CSP /login
  // 'error'    = 帳密登入失敗(登入頁顯示訊息)
  status: 'idle' | 'loading' | 'checking' | 'authed' | 'unauth' | 'error'
  error: string | null

  login: (username: string, password: string) => Promise<void>
  refresh: () => Promise<string | null>
  fetchMe: () => Promise<void>
  logout: () => Promise<void>
  hydrate: () => Promise<void>
}

// Drop any legacy zustand-persist blob that older builds wrote into
// localStorage (access + refresh tokens). Session is cookie-only now.
if (typeof localStorage !== 'undefined') {
  wipeAuthStorage(localStorage)
}

let profileInFlight: Promise<void> | null = null
let refreshInFlight: Promise<string | null> | null = null
let authEpoch = 0

export const useAuthStore = create<AuthState>()((set, get) => ({
  accessToken: null,
  refreshToken: null,
  user: null,
  status: 'idle',
  error: null,

  login: async (username, password) => {
    set({ status: 'loading', error: null })
    try {
      const { data } = await loginApi(username, password)
      // In-memory only for this page lifetime (Bearer interceptor).
      // Durable session is the httpOnly cookie CSP sets on login.
      set({
        accessToken: data.access_token,
        refreshToken: data.refresh_token,
        status: 'authed',
        error: null,
      })
      await get().fetchMe()
    } catch (err) {
      // explainError → same backend-derived message the UI shows, so any
      // consumer of state.error gets consistent text (not axios's generic
      // "Request failed with status code 401").
      set({ status: 'error', error: explainError(err) })
      throw err
    }
  },

  refresh: async () => {
    if (refreshInFlight) return refreshInFlight
    // Cookie-first: CSP accepts anila_refresh_token on /api/auth/refresh
    // even when the JSON body has no refresh_token (see password.py).
    // After a reload the in-memory copy is gone; the httpOnly cookie is not.
    const rt = get().refreshToken
    const epoch = authEpoch
    refreshInFlight = (async () => {
      try {
        const { data } = await refreshApi(rt)
        if (epoch === authEpoch) {
          set({ accessToken: data.access_token, refreshToken: data.refresh_token })
        }
        return data.access_token
      } catch {
        if (epoch === authEpoch) {
          set({ accessToken: null, refreshToken: null, user: null, status: 'unauth' })
        }
        return null
      } finally {
        refreshInFlight = null
      }
    })()
    return refreshInFlight
  },

  fetchMe: async () => {
    if (profileInFlight) return profileInFlight
    const epoch = ++authEpoch
    profileInFlight = (async () => {
      try {
        const { data } = await getMe()
        if (epoch === authEpoch) set({ user: data, status: 'authed' })
      } catch {
        if (epoch === authEpoch) {
          set({ accessToken: null, refreshToken: null, user: null, status: 'unauth' })
        }
      } finally {
        profileInFlight = null
      }
    })()
    return profileInFlight
  },

  logout: async () => {
    authEpoch += 1
    profileInFlight = null
    set({
      accessToken: null,
      refreshToken: null,
      user: null,
      status: 'unauth',
      error: null,
    })
    try {
      await logoutApi()
    } catch {
      // The local state is already clean; the login surface remains usable.
    }
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

// Wire the axios interceptor to the store. Done at module load — the
// store is created above synchronously, so by the time the first request
// fires the adapter is already in place.
bindAuthAdapter({
  getAccessToken: () => useAuthStore.getState().accessToken,
  refresh: () => useAuthStore.getState().refresh(),
  logout: () => useAuthStore.getState().logout(),
})
