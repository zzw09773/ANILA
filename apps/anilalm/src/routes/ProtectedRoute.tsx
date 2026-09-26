import { useEffect } from 'react'
import { Outlet } from 'react-router'
import { useAuthStore } from '../store/auth'

// ANILALM 不持有登入頁；唯一登入入口是治理中心 /login。
// 未登入時用 window.location.assign 跳出
// SPA，full page navigation 讓 nginx 把 /login 路由到 csp_backend serve
// LoginView.vue。React Router 的 <Navigate to="/login"> 行不通 — /login
// 不在 BrowserRouter (basename=/anilalm/) 的路由表內。
export function ProtectedRoute() {
  const status = useAuthStore((s) => s.status)

  // 跳轉只在「已探測且確定未登入」(status==='unauth')時發生。
  // 'idle'/'checking' 都是「還在確認」→ 渲染 null 等待,不跳轉;
  // 否則 cookie-based SSO 還沒驗完就把 user 踢回 CSP /login(原 bug)。
  // 看 status。工作階段在 httpOnly cookie，頁面不留 access／refresh token。
  useEffect(() => {
    if (status !== 'unauth') return
    // absolute URL with current port — ANILALM 可能跑在 4443，LoginView 在
    // 443；next 帶完整 URL (含 port)，登入完才能跨 port 跳回 ANILALM。
    const currentHref = window.location.href
    const loginOrigin = `${window.location.protocol}//${window.location.hostname}`
    const target = `${loginOrigin}/login?next=${encodeURIComponent(currentHref)}`
    window.location.assign(target)
  }, [status])

  if (status === 'authed') return <Outlet />
  return null
}
