import { useEffect } from 'react'
import { Outlet } from 'react-router'
import { useAuthStore } from '../store/auth'

// branch SSO：ANILALM 不再持有自己的登入頁；唯一登入入口是 myCSPPlatform
// CSP 平台 (路徑 /login)。Unauthenticated 時用 window.location.assign 跳出
// SPA，full page navigation 讓 nginx 把 /login 路由到 csp_backend serve
// LoginView.vue。React Router 的 <Navigate to="/login"> 行不通 — /login
// 不在 BrowserRouter (basename=/anilalm/) 的路由表內。
export function ProtectedRoute() {
  const status = useAuthStore((s) => s.status)

  // 跳轉只在「已探測且確定未登入」(status==='unauth')時發生。
  // 'idle'/'checking'/'loading' 都是「還在確認」→ 渲染 null 等待,不跳轉;
  // 否則 cookie-based SSO 還沒驗完就把 user 踢回 CSP /login(原 bug)。
  // 改看 status 而非 accessToken:跨 app SSO 接手時 token 留在 httpOnly
  // cookie,store 的 accessToken 為 null,但 fetchMe 會把 status 設 'authed'。
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
