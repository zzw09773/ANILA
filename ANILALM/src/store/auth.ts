import { create } from 'zustand'
import { persist, createJSONStorage } from 'zustand/middleware'
import { bindAuthAdapter } from '../api/client'
import { getMe, login as loginApi, logoutApi, refreshToken as refreshApi } from '../api/auth'
import type { UserMe } from '../types'

interface AuthState {
  accessToken: string | null
  refreshToken: string | null
  user: UserMe | null
  status: 'idle' | 'loading' | 'authed' | 'error'
  error: string | null

  login: (username: string, password: string) => Promise<void>
  refresh: () => Promise<string | null>
  fetchMe: () => Promise<void>
  logout: () => void
  hydrate: () => Promise<void>
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set, get) => ({
      accessToken: null,
      refreshToken: null,
      user: null,
      status: 'idle',
      error: null,

      login: async (username, password) => {
        set({ status: 'loading', error: null })
        try {
          const { data } = await loginApi(username, password)
          set({
            accessToken: data.access_token,
            refreshToken: data.refresh_token,
            status: 'authed',
            error: null,
          })
          await get().fetchMe()
        } catch (err) {
          set({ status: 'error', error: err instanceof Error ? err.message : String(err) })
          throw err
        }
      },

      refresh: async () => {
        const rt = get().refreshToken
        if (!rt) return null
        try {
          const { data } = await refreshApi(rt)
          set({ accessToken: data.access_token, refreshToken: data.refresh_token })
          return data.access_token
        } catch {
          set({ accessToken: null, refreshToken: null, user: null, status: 'idle' })
          return null
        }
      },

      fetchMe: async () => {
        try {
          const { data } = await getMe()
          set({ user: data, status: 'authed' })
        } catch {
          set({ accessToken: null, refreshToken: null, user: null, status: 'idle' })
        }
      },

      logout: () => {
        // Fire-and-forget: server-side logout is best-effort. The local
        // state reset is the source of truth — even if the network call
        // fails the user is signed out from the client's POV.
        void logoutApi().catch(() => undefined)
        set({
          accessToken: null,
          refreshToken: null,
          user: null,
          status: 'idle',
          error: null,
        })
      },

      hydrate: async () => {
        if (get().accessToken && !get().user) {
          await get().fetchMe()
        }
      },
    }),
    {
      name: 'anilalm:auth',
      storage: createJSONStorage(() => localStorage),
      partialize: (s) => ({
        accessToken: s.accessToken,
        refreshToken: s.refreshToken,
      }),
    },
  ),
)

// Wire the axios interceptor to the store. Done at module load — the
// store is created above synchronously, so by the time the first request
// fires the adapter is already in place.
bindAuthAdapter({
  getAccessToken: () => useAuthStore.getState().accessToken,
  refresh: () => useAuthStore.getState().refresh(),
  logout: () => useAuthStore.getState().logout(),
})
