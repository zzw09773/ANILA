import client from './client'

export const listMyAgents = () =>
  client.get('/api/agents')

export const getAgent = (id) =>
  client.get(`/api/agents/${id}`)

export const registerAgent = (data) =>
  client.post('/api/agents/register', data)

export const approveAgent = (id) =>
  client.post(`/api/agents/${id}/approve`)

export const rejectAgent = (id, reason = '') =>
  client.post(`/api/agents/${id}/reject`, { reason })

export const setAgentEncryption = (id, requires_encryption) =>
  client.post(`/api/agents/${id}/encryption`, { requires_encryption })

export const downloadTemplate = () =>
  client.get('/api/agents/template/download', { responseType: 'blob' })

export const deleteAgent = (id) =>
  client.delete(`/api/agents/${id}`)

export const triggerAgentHealthCheck = (id) =>
  client.post(`/api/agents/${id}/health-check`)

// Owner / admin — patch any of endpoint / description / capabilities /
// api_version / base_model_id / input_schema. Name and approval_status
// are intentionally not updatable from here.
export const updateAgent = (id, patch) =>
  client.put(`/api/agents/${id}`, patch)

// Sprint 13 PR A3 — per-agent runtime config (tool permissions,
// workspace caps, guardrails). Agents poll their own copy via
// X-CSP-Service-Token at /api/agents/me/runtime-config; this admin
// surface uses owner / admin auth.
export const getAgentRuntimeConfig = (id) =>
  client.get(`/api/agents/${id}/runtime-config`)

export const setAgentRuntimeConfig = (id, runtime_config) =>
  client.patch(`/api/agents/${id}/runtime-config`, { runtime_config })
