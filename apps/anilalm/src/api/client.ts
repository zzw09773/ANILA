import axios, { AxiosError, type InternalAxiosRequestConfig } from 'axios'

// 全站共用一個 axios。工作階段是 httpOnly cookie，攔截器只補 CSRF、
// 在 401 時換發。不能在這裡直接 import auth store：store 會 import
// 用到這個檔案的 api。用 adapter 把依賴方向保持單向。

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
  // 只回是否換到新的 httpOnly cookie。權杖留在瀏覽器，不進頁面記憶體。
  refresh(): Promise<boolean>
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

// 變更類的 cookie 請求要回帶 anila_csrf。沒有這顆 cookie 時不送標頭。
export function csrfHeader(): Record<string, string> {
  const csrf = readCookie(CSRF_COOKIE)
  return csrf ? { 'X-CSRF-Token': csrf } : {}
}

client.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  // 工作階段只走 httpOnly cookie（withCredentials）。不附 Authorization。
  // 變更類請求要帶 X-CSRF-Token，值與非 httpOnly 的 anila_csrf cookie 相同。
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
        const ok = await adapter.refresh()
        if (!ok) {
          adapter.logout()
          return Promise.reject(error)
        }
        return client(original)
      } catch (refreshErr) {
        adapter.logout()
        return Promise.reject(refreshErr instanceof Error ? refreshErr : error)
      }
    }
    return Promise.reject(error)
  },
)

/**
 * 與 axios client 同一套 cookie 工作階段。給不走 axios 的串流與二進位請求用。
 * 401 時用 refresh cookie 換發一次再送，不把權杖留在頁面裡。
 */
export async function fetchWithSession(
  input: string,
  init: RequestInit = {},
): Promise<Response> {
  const method = (init.method ?? 'GET').toLowerCase()
  const send = () => {
    // 每次送出都重讀 cookie。refresh 會換發 anila_csrf；沿用舊標頭會被拒成 403。
    const headers = new Headers(init.headers)
    if (MUTATING_METHODS.has(method)) {
      for (const [key, value] of Object.entries(csrfHeader())) {
        headers.set(key, value)
      }
    }
    return fetch(input, {
      ...init,
      credentials: 'include',
      headers,
    })
  }
  let res = await send()
  if (res.status !== 401 || !adapter || init.signal?.aborted) return res
  try {
    await res.body?.cancel()
  } catch {
    // 第一個 401 的 body 沒人讀。取消失敗就讓它自己關掉。
  }
  const ok = await adapter.refresh()
  if (!ok) {
    adapter.logout()
    return res
  }
  return send()
}

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
