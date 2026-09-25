import axios, { AxiosError, type InternalAxiosRequestConfig } from 'axios'

// Single axios instance reused everywhere. Bearer JWT injected by an
// interceptor; the auth store owns the tokens and is set via
// `bindAuthAdapter` after the store is initialised. We can't import the
// store here directly because the store imports api modules that import
// this file — circular. The adapter pattern keeps the dep graph clean.

/**
 * Base URL for the anila-studio service.
 *
 * Studio is its own FastAPI process — separate from the CSP backend
 * that serves `/api/auth`, `/api/chat`, etc. Nginx (and the Vite proxy)
 * send the same five prefixes to anila-studio: `/api/studio`,
 * `/api/reports`, `/api/mindmaps`, `/api/infographics`, `/api/datatables`.
 * In local dev those land on `http://localhost:8100`, so the default
 * empty string is correct. Production builds are usually served
 * same-origin behind that nginx. Only set this env var when the SPA
 * needs to call studio cross-origin (staging boxes, preview deploys).
 *
 * Note: this constant is consumed by `src/api/studio.ts`. Other API
 * modules continue to use empty-string baseURL via the shared `client`
 * axios instance — csp traffic must NOT be rerouted here.
 */
export const STUDIO_BASE_URL: string = import.meta.env.VITE_STUDIO_BASE_URL ?? ''

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

const CSRF_COOKIE = 'anila_csrf'
const MUTATING_METHODS = new Set(['post', 'put', 'patch', 'delete'])

function readCookie(name: string): string | null {
  if (typeof document === 'undefined') return null
  const match = document.cookie.match(new RegExp(`(?:^|; )${name}=([^;]*)`))
  return match ? decodeURIComponent(match[1]) : null
}

// Double-submit CSRF header for mutating cookie-auth requests. Exported so
// the streaming chat path (api/chat.ts) — which bypasses this axios client —
// can attach the same header. Empty when the cookie is absent (e.g. pure
// Bearer flows, which are CSRF-exempt server-side anyway).
export function csrfHeader(): Record<string, string> {
  const csrf = readCookie(CSRF_COOKIE)
  return csrf ? { 'X-CSRF-Token': csrf } : {}
}

client.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  const token = adapter?.getAccessToken()
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  // Double-submit CSRF: when there's no Bearer (e.g. the in-memory access token
  // expired/cleared but the httpOnly session cookie lingers), CSP's CSRF
  // middleware requires X-CSRF-Token to match the non-httpOnly `anila_csrf`
  // cookie on mutating requests. Bearer requests are CSRF-exempt server-side,
  // so sending it always is harmless. Fixes logout 403 after token expiry.
  const method = (config.method ?? 'get').toLowerCase()
  if (MUTATING_METHODS.has(method)) {
    const csrf = readCookie(CSRF_COOKIE)
    if (csrf) config.headers['X-CSRF-Token'] = csrf
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
// `{detail: "..."}` or an endpoint-specific detail object on errors;
// fall back to status + message only when neither has a display message.
export function httpStatus(err: unknown): number | undefined {
  if (axios.isAxiosError(err)) return err.response?.status
  return undefined
}

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
    if (
      detail &&
      typeof detail === 'object' &&
      typeof (detail as { message?: unknown }).message === 'string'
    ) {
      const message = (detail as { message: string }).message.trim()
      if (message) return message
    }
    if (err.response?.status) return `${err.response.status} ${err.message}`
    return err.message
  }
  if (err instanceof Error) return err.message
  return String(err)
}
