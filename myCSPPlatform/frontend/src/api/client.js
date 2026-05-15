import axios from 'axios'
import { useAuthStore } from '../stores/auth'
import router from '../router'

// Sprint 5 X / H5: 全面改走 backend 既有的 httpOnly cookie 流程，停止把
// access / refresh token 寫進 localStorage（XSS 即洩漏的 7 天 refresh
// token）。後端在 /api/auth/login + /refresh + OIDC callback 都已 set
// `anila_access_token` (httpOnly) / `anila_refresh_token` (httpOnly,
// path=/api/auth/refresh) / `anila_csrf` (non-httpOnly, double-submit
// CSRF) 三個 cookie；前端只要：
//   1. 開啟 withCredentials 讓瀏覽器自動帶 cookie
//   2. mutating request 從 anila_csrf cookie 讀值並 echo 到 X-CSRF-Token

const CSRF_COOKIE_NAME = 'anila_csrf'
const CSRF_HEADER_NAME = 'X-CSRF-Token'
const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS'])

function readCookie(name) {
  if (typeof document === 'undefined') return null
  const target = `${name}=`
  for (const part of document.cookie.split(';')) {
    const trimmed = part.trim()
    if (trimmed.startsWith(target)) {
      return decodeURIComponent(trimmed.slice(target.length))
    }
  }
  return null
}

const client = axios.create({
  baseURL: '',
  headers: {
    'Content-Type': 'application/json',
  },
  // 讓瀏覽器在 same-origin / 已設 Access-Control-Allow-Credentials 的
  // 跨來源請求中自動帶 cookie。後端 ALLOWED_ORIGINS 已限制可用來源。
  withCredentials: true,
})

// Request interceptor: 不再附 Authorization header（cookie 自動帶）；
// 對 mutating 請求補 CSRF header。
client.interceptors.request.use((config) => {
  const method = (config.method || 'get').toUpperCase()
  if (!SAFE_METHODS.has(method)) {
    const csrf = readCookie(CSRF_COOKIE_NAME)
    if (csrf) {
      config.headers[CSRF_HEADER_NAME] = csrf
    }
  }
  return config
})

// Response interceptor: 401 → 嘗試 refresh（cookie 流程不需要 body 傳
// refresh_token，後端會從 anila_refresh_token cookie 讀）。
//
// 三條「不 retry」白名單避免 infinite loop / 不必要的 redirect：
//   - /api/auth/refresh 本身 401：cookie 已死，重試只會再 401，每次新
//     request 都是新 config object，``_retry`` flag 跨不過去。會造成無限
//     遞迴直到後端 rate-limit 回 503。直接 reject 讓 caller 處理。
//   - /api/auth/login / /card/verify 401：是「credential 錯」，refresh
//     沒意義。
//   - 已在 /login 頁：再 router.push('/login') 也沒影響，但會觸發
//     redundant navigation lifecycle，浪費。
const NO_RETRY_PATHS = [
  '/api/auth/refresh',
  '/api/auth/login',
  '/api/auth/card/verify',
]

client.interceptors.response.use(
  (response) => response,
  async (error) => {
    const originalRequest = error.config
    if (!originalRequest) return Promise.reject(error)

    const url = originalRequest.url || ''
    const skipRetry = NO_RETRY_PATHS.some((p) => url.includes(p))

    if (error.response?.status === 401 && !skipRetry && !originalRequest._retry) {
      originalRequest._retry = true
      const authStore = useAuthStore()

      try {
        await authStore.refreshToken()
        return client(originalRequest)
      } catch {
        authStore.logout()
        if (router.currentRoute.value?.path !== '/login') {
          router.push('/login')
        }
        return Promise.reject(error)
      }
    }

    return Promise.reject(error)
  }
)

export default client
