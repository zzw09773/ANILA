import client from './client'

export const listAuthProviders = () =>
  client.get('/api/auth-providers')

export const createAuthProvider = (data) =>
  client.post('/api/auth-providers', data)

export const updateAuthProvider = (id, data) =>
  client.put(`/api/auth-providers/${id}`, data)

export const deactivateAuthProvider = (id) =>
  client.delete(`/api/auth-providers/${id}`)
