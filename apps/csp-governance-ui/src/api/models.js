import client from './client'

export const listModels = () =>
  client.get('/api/models')

export const createModel = (data) =>
  client.post('/api/models', data)

export const updateModel = (id, data) =>
  client.put(`/api/models/${id}`, data)

export const deleteModel = (id) =>
  client.delete(`/api/models/${id}`)

export const activateModel = (id) =>
  client.post(`/api/models/${id}/activate`)

export const purgeModel = (id) =>
  client.delete(`/api/models/${id}/purge`)

export const triggerHealthCheck = (id) =>
  client.post(`/api/models/${id}/health-check`)

export const setRouterPrimary = (id) =>
  client.post(`/api/models/${id}/set-router-primary`)

export const unsetRouterPrimary = (id) =>
  client.post(`/api/models/${id}/unset-router-primary`)
