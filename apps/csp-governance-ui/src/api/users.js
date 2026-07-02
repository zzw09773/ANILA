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

// branch SSO: 永久刪除使用者 (跟 deactivate 對照)。
// 後端會 cascade 刪 api_keys、保留 audit_logs (actor 設 NULL)。
// 擁有 agents 的 user 會被拒，admin 要先處理 agent 擁有權。
export const hardDeleteUser = (id) =>
  client.delete(`/api/users/${id}/permanent`)

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
