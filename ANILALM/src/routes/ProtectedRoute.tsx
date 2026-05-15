import { useEffect } from 'react'
import { Outlet } from 'react-router-dom'
import { useAuthStore } from '../store/auth'

// branch SSO：ANILALM 不再持有自己的登入頁；唯一登入入口是 myCSPPlatform
// CSP 平台 (路徑 /login)。Unauthenticated 時用 window.location.assign 跳出
// SPA，full page navigation 讓 nginx 把 /login 路由到 csp_backend serve
// LoginView.vue。React Router 的 <Navigate to="/login"> 行不通 — /login
// 不在 BrowserRouter (basename=/anilalm/) 的路由表內。
export function ProtectedRoute() {
  const accessToken = useAuthStore((s) => s.accessToken)
  const status = useAuthStore((s) => s.status)

  // 用 absolute URL with current port — ANILALM 可能跑在 4443，但 LoginView
  // 在 443。next 帶完整 URL (含 port)，登入完才能跨 port 跳回 ANILALM。
  useEffect(() => {
    if (accessToken) return
    const currentHref = window.location.href
    const loginOrigin = `${window.location.protocol}//${window.location.hostname}`
    const target = `${loginOrigin}/login?next=${encodeURIComponent(currentHref)}`
    window.location.assign(target)
  }, [accessToken])

  if (!accessToken) return null
  if (status === 'loading') return null
  return <Outlet />
}
