import axios, { AxiosError, type InternalAxiosRequestConfig } from 'axios'

// Single axios instance reused everywhere. Bearer JWT injected by an
// interceptor; the auth store owns the tokens and is set via
// `bindAuthAdapter` after the store is initialised. We can't import the
// store here directly because the store imports api modules that import
// this file — circular. The adapter pattern keeps the dep graph clean.

interface AuthAdapter {
  getAccessToken(): string | null
  refresh(): Promise<string | null>
  logout(): void
}

let adapter: AuthAdapter | null = null

export function bindAuthAdapter(a: AuthAdapter) {
  adapter = a
}

export const client = axios.create({
  // Empty base URL → the dev proxy or production same-origin handles
  // both /api and /v1 routes.
  baseURL: '',
  withCredentials: true, // also send the CSP cookie for SSE/job streaming
  headers: { 'Content-Type': 'application/json' },
})

client.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  const token = adapter?.getAccessToken()
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

interface RetriableConfig extends InternalAxiosRequestConfig {
  _retry?: boolean
}

// Paths that MUST NOT trigger a refresh on 401, otherwise we'd recurse
// (the refresh endpoint itself uses this same client). Login/logout 401s
// are also user-facing failures that the UI should show verbatim.
const NO_REFRESH_PATHS = ['/api/auth/refresh', '/api/auth/login', '/api/auth/logout']

client.interceptors.response.use(
  (response) => response,
  async (error: AxiosError) => {
    if (!adapter) return Promise.reject(error)
    const original = error.config as RetriableConfig | undefined
    if (!original) return Promise.reject(error)

    const url = original.url ?? ''
    const skipRefresh = NO_REFRESH_PATHS.some((p) => url.startsWith(p))

    if (error.response?.status === 401 && !original._retry && !skipRefresh) {
      original._retry = true
      try {
        const newToken = await adapter.refresh()
        if (!newToken) throw new Error('refresh returned no token')
        original.headers.Authorization = `Bearer ${newToken}`
        return client(original)
      } catch (refreshErr) {
        adapter.logout()
        return Promise.reject(refreshErr instanceof Error ? refreshErr : error)
      }
    }
    return Promise.reject(error)
  },
)

// Pretty-format an axios error for toast messages. CSP backend returns
// `{detail: "..."}` on errors; fall back to status + message.
export function explainError(err: unknown): string {
  if (axios.isAxiosError(err)) {
    const detail = (err.response?.data as { detail?: unknown } | undefined)?.detail
    if (typeof detail === 'string') return detail
    if (Array.isArray(detail)) {
      // pydantic validation errors come back as a list
      return detail
        .map((d) => (typeof d === 'string' ? d : (d as { msg?: string }).msg ?? JSON.stringify(d)))
        .join('; ')
    }
    if (err.response?.status) return `${err.response.status} ${err.message}`
    return err.message
  }
  if (err instanceof Error) return err.message
  return String(err)
}
