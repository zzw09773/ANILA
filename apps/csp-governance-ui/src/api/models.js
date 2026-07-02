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

// Slice 6b — Model Gateway hardening。GET 讀最近一次探測結果的五態快照；
// POST /test 觸發主動探測（active probe），回 { health_status, latency_ms }。
export const getModelHealth = (id) =>
  client.get(`/api/models/${id}/health`)

export const testModelConnection = (id) =>
  client.post(`/api/models/${id}/test`)

export const setRouterPrimary = (id) =>
  client.post(`/api/models/${id}/set-router-primary`)

export const unsetRouterPrimary = (id) =>
  client.post(`/api/models/${id}/unset-router-primary`)
