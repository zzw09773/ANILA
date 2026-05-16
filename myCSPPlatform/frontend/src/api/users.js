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

// Owner-only, irreversible. Backend rejects (409) if user owns any agent.
export const purgeUser = (id) =>
  client.delete(`/api/users/${id}/purge`)

export const getMyAllowedModels = () =>
  client.get('/api/users/me/allowed-models')

export const getUserAllowedModels = (id) =>
  client.get(`/api/users/${id}/allowed-models`)

export const updateUserAllowedModels = (id, modelIds) =>
  client.put(`/api/users/${id}/allowed-models`, { model_ids: modelIds })

export const getUserAllowedAgents = (id) =>
  client.get(`/api/users/${id}/allowed-agents`)

export const updateUserAllowedAgents = (id, agentIds) =>
  client.put(`/api/users/${id}/allowed-agents`, { agent_ids: agentIds })
