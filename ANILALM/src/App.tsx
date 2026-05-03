import { useEffect } from 'react'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { ThemeProvider } from './theme/ThemeContext'
import { useAuthStore } from './store/auth'
import { LoginPage } from './routes/LoginPage'
import { DashboardPage } from './routes/DashboardPage'
import { WorkspacePage } from './routes/WorkspacePage'
import { ProtectedRoute } from './routes/ProtectedRoute'
import { ErrorBoundary } from './components/ErrorBoundary'

function AppRoutes() {
  const hydrate = useAuthStore((s) => s.hydrate)
  useEffect(() => {
    void hydrate()
  }, [hydrate])

  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route element={<ProtectedRoute />}>
        <Route path="/" element={<DashboardPage />} />
        <Route path="/c/:collectionId" element={<WorkspacePage />} />
        <Route path="/c/:collectionId/conv/:conversationId" element={<WorkspacePage />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
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
