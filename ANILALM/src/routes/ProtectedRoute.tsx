import { useMemo } from 'react'
import { Navigate, Outlet, useLocation } from 'react-router-dom'
import { useAuthStore } from '../store/auth'

export function ProtectedRoute() {
  const accessToken = useAuthStore((s) => s.accessToken)
  const status = useAuthStore((s) => s.status)
  const location = useLocation()

  // The `state` object passed to <Navigate> goes into <react-router>'s
  // internal `useEffect` deps. A bare object literal — `{{ from: location }}` —
  // is a NEW reference every render, which makes the effect treat each
  // render as "deps changed" and re-call navigate(). Combined with React's
  // batching that turns into a render→navigate→render loop and trips
  // "Maximum update depth exceeded" (#185).
  // Memoising on `location.pathname` gives us a stable reference per URL.
  const navState = useMemo(
    () => ({ from: { pathname: location.pathname, search: location.search } }),
    [location.pathname, location.search],
  )

  if (!accessToken) {
    return <Navigate to="/login" replace state={navState} />
  }
  if (status === 'loading') return null
  return <Outlet />
}
