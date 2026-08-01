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

// G9: set the agent's default classification level (four-level vocabulary).
// Legacy requires_encryption is derived server-side (level ≥ 密).
export const setAgentClassification = (id, default_classification_level) =>
  client.post(`/api/agents/${id}/classification`, { default_classification_level })

export const downloadTemplate = () =>
  client.get('/api/agents/template/download', { responseType: 'blob' })

// P2.1 — public CSPKI CA bundle for JWKS over https (agent-side trust anchor).
// Backend route required: GET /api/agents/platform-ca/download → application/x-pem-file
// (serves services/csp/app/services/cspki_ca_bundle.pem). Not implemented in this package.
export const downloadPlatformCa = () =>
  client.get('/api/agents/platform-ca/download', { responseType: 'blob' })

// P2.1 — single-file dispatch JWT verifier for existing Python agents.
// Backend route required: GET /api/agents/anila-verify/download
// → text/x-python (or application/octet-stream), filename=anila_verify.py
// Source of truth: packages/anila-core/.../contrib/anila_verify.py (served by CSP).
// Not implemented in this package — UI degrades honestly on 404.
export const downloadAnilaVerify = () =>
  client.get('/api/agents/anila-verify/download', { responseType: 'blob' })

export const deleteAgent = (id) =>
  client.delete(`/api/agents/${id}`)

export const triggerAgentHealthCheck = (id) =>
  client.post(`/api/agents/${id}/health-check`)

// P2.1 — probe with a signed dispatch JWT (same path as live dispatch).
// Returns three facts separately: host_reachable / credentials_accepted /
// path_verified (null = could not be determined — never collapse to one bool).
export const testAgentConnection = (id) =>
  client.post(`/api/agents/${id}/test-connection`)

// Owner / admin — patch endpoint / description / api_version /
// base_model_id / input_schema / classification / collections.
// Name, approval_status, capabilities, and classification_ceiling are
// not updatable here (latter two retired as accept-and-ignore controls).
export const updateAgent = (id, patch) =>
  client.put(`/api/agents/${id}`, patch)

// Runtime-config admin writes are retired (PATCH → 410). GET remains for
// read-only inspection of any historically stored JSON. The governance
// view no longer calls PATCH — do not re-add a write helper that the UI
// would present as a working control.
export const getAgentRuntimeConfig = (id) =>
  client.get(`/api/agents/${id}/runtime-config`)

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
