import { useEffect } from 'react'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router'
import { ThemeProvider } from './theme/ThemeContext'
import { useAuthStore } from './store/auth'
// 唯一登入頁是治理中心 /login。ProtectedRoute 在未登入時整頁導過去。
import { DashboardPage } from './routes/DashboardPage'
import { WorkspacePage } from './routes/WorkspacePage'
import { ProtectedRoute } from './routes/ProtectedRoute'
import { NotFoundPage } from './routes/NotFoundPage'
import { ErrorBoundary } from './components/ErrorBoundary'

// 空節點。對話 id 放在子路由，WorkspacePage 才不會在第一則訊息時被換成另一個實例。
function WorkspaceConversationRoute() {
  return null
}

export function AppRoutes() {
  const hydrate = useAuthStore((s) => s.hydrate)
  useEffect(() => {
    void hydrate()
  }, [hydrate])

  return (
    <Routes>
      <Route element={<ProtectedRoute />}>
        <Route path="/" element={<DashboardPage />} />
        <Route path="/c/:collectionId" element={<WorkspacePage />}>
          <Route index element={<WorkspaceConversationRoute />} />
          <Route path="conv/:conversationId" element={<WorkspaceConversationRoute />} />
        </Route>
        <Route path="/conv/:conversationId" element={<WorkspacePage />} />
      </Route>
      <Route path="*" element={<NotFoundPage />} />
    </Routes>
  )
}

// BASE_URL comes from Vite's `base` config. When ANILALM runs at root
// (local dev) it's just '/'; when mounted under '/anilalm/' behind the
// ANILA reverse proxy it's '/anilalm/'. React Router wants a basename
// without the trailing slash and treats '/' the same as ''.
const ROUTER_BASENAME = import.meta.env.BASE_URL.replace(/\/$/, '') || undefined

export function App() {
  return (
    <ErrorBoundary>
      <ThemeProvider>
        <BrowserRouter basename={ROUTER_BASENAME}>
          <AppRoutes />
        </BrowserRouter>
      </ThemeProvider>
    </ErrorBoundary>
  )
}
