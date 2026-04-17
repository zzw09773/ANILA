import client from './client'

export const listUsers = () =>
  client.get('/api/users')

export const createUser = (data) =>
  client.post('/api/users', data)

export const updateUser = (id, data) =>
  client.put(`/api/users/${id}`, data)

export const resetUserPassword = (id, data) =>
  client.post(`/api/users/${id}/reset-password`, data)

export const deactivateUser = (id) =>
  client.delete(`/api/users/${id}`)

export const getMyAllowedModels = () =>
  client.get('/api/users/me/allowed-models')

export const getUserAllowedModels = (id) =>
  client.get(`/api/users/${id}/allowed-models`)

export const updateUserAllowedModels = (id, modelIds) =>
  client.put(`/api/users/${id}/allowed-models`, { model_ids: modelIds })
