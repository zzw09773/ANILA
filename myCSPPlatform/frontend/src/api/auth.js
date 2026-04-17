import client from './client'

export const login = (username, password, extra = {}) =>
  client.post('/api/auth/login', { username, password, ...extra })

export const refreshTokenApi = (refresh_token) =>
  client.post('/api/auth/refresh', { refresh_token })

export const getMe = () =>
  client.get('/api/auth/me')

export const changePassword = (current_password, new_password) =>
  client.put('/api/auth/password', { current_password, new_password })

export const register = (username, email, password) =>
  client.post('/api/auth/register', { username, email, password })

export const listPublicAuthProviders = () =>
  client.get('/api/auth/providers')

export const getOidcStartUrl = (providerId, nextPath = '/') =>
  client.get(`/api/auth/oidc/${providerId}/start`, { params: { next_path: nextPath } })
