import { useEffect } from 'react'
import { Outlet } from 'react-router'
import { useAuthStore } from '../store/auth'
import { cspLoginHref } from './loginRedirect'

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
    // 相對網址。用 hostname 重組絕對網址會拿掉埠，8443 會被送去 443。
    window.location.assign(cspLoginHref(window.location))
  }, [status])

  if (status === 'authed') return <Outlet />
  return null
}
