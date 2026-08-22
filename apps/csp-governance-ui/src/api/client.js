import axios from 'axios'
import { useAuthStore } from '../stores/auth'
import { loginHref } from '../utils/appOrigins'

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
  withCredentials: true,
})

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

const NO_RETRY_PATHS = [
  '/api/auth/refresh',
  '/api/auth/login',
  '/api/auth/logout',
  '/api/auth/refresh/logout',
  '/api/auth/card/verify',
]

function onLoginSurface() {
  return window.location.pathname === '/login'
    || window.location.pathname.endsWith('/login')
}

client.interceptors.response.use(
  (response) => response,
  async (error) => {
    const originalRequest = error.config
    if (!originalRequest) return Promise.reject(error)

    const url = originalRequest.url || ''
    const skipRetry = NO_RETRY_PATHS.some((p) => url.includes(p))

    if (error.response?.status === 401 && !skipRetry && !originalRequest._retry) {
      // /login must not mint a new access token from a leftover refresh
      // cookie. POST /logout expires those cookies; this is the belt if
      // one still arrives. A later visit to bare /login then stays on
      // the form instead of bouncing to /app.
      if (onLoginSurface()) {
        return Promise.reject(error)
      }
      originalRequest._retry = true
      const authStore = useAuthStore()

      try {
        await authStore.refreshToken()
        return client(originalRequest)
      } catch {
        await authStore.logout()
        if (!onLoginSurface()) {
          window.location.replace(loginHref())
        }
        return Promise.reject(error)
      }
    }

    return Promise.reject(error)
  }
)

export default client
