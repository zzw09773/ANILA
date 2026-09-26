import { client } from './client'
import type { UserMe } from '../types'

// 換發只靠 httpOnly anila_refresh_token cookie。不把 refresh token 放進
// 請求本文，回應裡的權杖也不留給呼叫端保存。
export const refreshToken = () => client.post('/api/auth/refresh', {})

export const getMe = () => client.get<UserMe>('/api/auth/me')

export const logoutApi = () => client.post('/api/auth/logout', {})
