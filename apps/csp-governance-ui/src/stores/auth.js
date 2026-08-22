import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { login as loginApi, refreshTokenApi, getMe, logout as logoutApi } from '../api/auth'
import { loginWithCard as runCardLogin } from '../api/caAuth'
import { loginHref } from '../utils/appOrigins'
import {
  normalizeSessionProfile,
  shouldApplySessionProfile,
} from '../utils/sessionProfile'

// Cookie-only auth store. Tokens stay in httpOnly cookies; the SPA only
// remembers the current User from GET /api/auth/me.
//
// Session restore is single-flight + epoch-guarded: two overlapping /me
// calls (store bootstrap vs router guard vs 401 retry) used to race, and
// the loser could overwrite a good user with null — testers saw the role
// flip between developer / user / admin on refresh.
//
// A later /me must not invent role=user (TokenResponse / missing role)
// or downgrade the same id. logout() still bumps the epoch so a late
// /me cannot restore anyone after 登出.
export const useAuthStore = defineStore('auth', () => {
  const user = ref(null)
  const initialized = ref(false)

  let fetchEpoch = 0
  let fetchInFlight = null
  let refreshInFlight = null

  const isAuthenticated = computed(() => !!user.value)
  const isOwner = computed(() => user.value?.role === 'owner')
  const isAdmin = computed(() =>
    user.value?.role === 'admin' || user.value?.role === 'owner',
  )
  const isDeveloper = computed(() =>
    user.value?.role === 'developer'
    || user.value?.role === 'admin'
    || user.value?.role === 'owner',
  )
  const isRegularUser = computed(() =>
    !!user.value && !isDeveloper.value,
  )

  async function login(username, password, extra = {}) {
    await loginApi(username, password, extra)
    await fetchUser({ force: true })
  }

  async function loginWithCard({ pin, componentOrigin } = {}) {
    const result = await runCardLogin({ pin, componentOrigin })
    if (result.status === 'ok') {
      await fetchUser({ force: true })
    }
    return result
  }

  async function refreshToken() {
    if (refreshInFlight) return refreshInFlight
    refreshInFlight = refreshTokenApi()
      .finally(() => {
        refreshInFlight = null
      })
    return refreshInFlight
  }

  async function fetchUser(options = {}) {
    const force = Boolean(options.force)
    if (fetchInFlight && !force) return fetchInFlight

    const epoch = ++fetchEpoch
    const pending = (async () => {
      try {
        const { data } = await getMe()
        if (epoch !== fetchEpoch) return
        const profile = normalizeSessionProfile(data)
        if (!profile) {
          if (force || !user.value) user.value = null
          return
        }
        if (!shouldApplySessionProfile(user.value, profile)) return
        user.value = profile
      } catch {
        if (epoch !== fetchEpoch) return
        user.value = null
      } finally {
        if (epoch === fetchEpoch) {
          initialized.value = true
        }
      }
    })()

    fetchInFlight = pending
    try {
      await pending
    } finally {
      if (fetchInFlight === pending) fetchInFlight = null
    }
  }

  async function logout() {
    fetchEpoch += 1
    fetchInFlight = null
    // Clear what the user can see before waiting on the network. The server
    // call below invalidates httpOnly cookies; the immediate local reset keeps
    // metrics and privileged chrome from surviving during a slow request.
    user.value = null
    initialized.value = true
    try {
      await logoutApi()
    } catch {
      // Backend logout failure must not leave a ghost session on screen.
    }
  }

  function hardRedirectToLogin() {
    window.location.replace(loginHref())
  }

  return {
    user,
    initialized,
    isAuthenticated,
    isOwner,
    isAdmin,
    isDeveloper,
    isRegularUser,
    login,
    loginWithCard,
    refreshToken,
    fetchUser,
    logout,
    hardRedirectToLogin,
  }
})
