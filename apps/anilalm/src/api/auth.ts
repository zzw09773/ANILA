import { client } from './client'
import type { TokenResponse, UserMe } from '../types'

export const login = (username: string, password: string) =>
  client.post<TokenResponse>('/api/auth/login', { username, password })

export const refreshToken = (refresh_token: string) =>
  client.post<TokenResponse>('/api/auth/refresh', { refresh_token })

export const getMe = () => client.get<UserMe>('/api/auth/me')

export const logoutApi = () => client.post('/api/auth/logout', {})
