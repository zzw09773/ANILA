import client from './client'

export const listMyAgents = () =>
  client.get('/api/agents')

export const getAgent = (id) =>
  client.get(`/api/agents/${id}`)

export const registerAgent = (data) =>
  client.post('/api/agents/register', data)

export const approveAgent = (id) =>
  client.post(`/api/agents/${id}/approve`)

// On-demand trace diagnostic (OE-1: no longer an approval gate).
export const traceTestAgent = (id) =>
  client.post(`/api/agents/${id}/trace-test`)

export const rejectAgent = (id, reason = '') =>
  client.post(`/api/agents/${id}/reject`, { reason })

// G9: set the agent's default classification level (four-level vocabulary).
// Legacy requires_encryption is derived server-side (level ≥ 密).
export const setAgentClassification = (id, default_classification_level) =>
  client.post(`/api/agents/${id}/classification`, { default_classification_level })

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

// Per-agent functions (2026-06-11, extensible) — developer-designed,
// surfaced in the ANILA chat UI for the active agent. kind + config.
export const listAgentFunctions = (ref) =>
  client.get(`/api/agents/${ref}/functions`)

export const createAgentFunction = (ref, data) =>
  client.post(`/api/agents/${ref}/functions`, data)

export const updateAgentFunction = (ref, fid, data) =>
  client.put(`/api/agents/${ref}/functions/${fid}`, data)

export const deleteAgentFunction = (ref, fid) =>
  client.delete(`/api/agents/${ref}/functions/${fid}`)
