import client from './client'

export const listPlatformLinks = (params) =>
  client.get('/api/platform-links', { params })

export const createPlatformLink = (data) =>
  client.post('/api/platform-links', data)

export const updatePlatformLink = (id, data) =>
  client.put(`/api/platform-links/${id}`, data)

export const deletePlatformLink = (id) =>
  client.delete(`/api/platform-links/${id}`)
