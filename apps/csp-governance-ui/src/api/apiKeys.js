import client from './client'

export const listApiKeys = () =>
  client.get('/api/keys')

export const createApiKey = (data) =>
  client.post('/api/keys', data)

export const getApiKey = (id) =>
  client.get(`/api/keys/${id}`)

export const updateApiKey = (id, data) =>
  client.put(`/api/keys/${id}`, data)

export const revokeApiKey = (id) =>
  client.delete(`/api/keys/${id}`)

export const regenerateApiKey = (id) =>
  client.post(`/api/keys/${id}/regenerate`)
