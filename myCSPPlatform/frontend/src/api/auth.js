import client from './client'

export const login = (username, password) =>
  client.post('/api/auth/login', { username, password })

// cookie 流程：refresh token 從 anila_refresh_token cookie 取，
// 不需 body；保留無參數 signature 以便未來 SDK 可選擇傳入。
export const refreshTokenApi = () =>
  client.post('/api/auth/refresh', {})

export const getMe = () =>
  client.get('/api/auth/me')

export const logout = () =>
  client.post('/api/auth/logout', {})

export const changePassword = (current_password, new_password) =>
  client.put('/api/auth/password', { current_password, new_password })

export const register = (username, email, password) =>
  client.post('/api/auth/register', { username, email, password })
