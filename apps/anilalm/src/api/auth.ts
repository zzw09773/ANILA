import { client } from './client'
import type { TokenResponse, UserMe } from '../types'

export const login = (username: string, password: string) =>
  client.post<TokenResponse>('/api/auth/login', { username, password })

// Body token is optional: after a reload the in-memory copy is gone and
// CSP reads the httpOnly ``anila_refresh_token`` cookie instead (path
// ``/api/auth/refresh``). withCredentials on the shared client sends it.
export const refreshToken = (refresh_token?: string | null) =>
  client.post<TokenResponse>(
    '/api/auth/refresh',
    refresh_token ? { refresh_token } : {},
  )

export const getMe = () => client.get<UserMe>('/api/auth/me')

export const logoutApi = () =>
  Promise.allSettled([
    client.post('/api/auth/refresh/logout', {}, { timeout: 4000 }),
    client.post('/api/auth/logout', {}, { timeout: 4000 }),
  ])
