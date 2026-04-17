import client from './client'

export const listModels = () =>
  client.get('/api/models')

export const createModel = (data) =>
  client.post('/api/models', data)

export const updateModel = (id, data) =>
  client.put(`/api/models/${id}`, data)

export const deleteModel = (id) =>
  client.delete(`/api/models/${id}`)

export const triggerHealthCheck = (id) =>
  client.post(`/api/models/${id}/health-check`)
