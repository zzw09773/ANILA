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

// 快速起步骨架。不帶 agent_id 是預設通用包（此時還沒有 endpoint，不能先註冊）。
// 帶數字 id 是選用：預填該 agent 的 ANILA_AGENT_ID，並由後端驗 owner/admin。
export const downloadQuickstart = (agentId) => {
  const config = { responseType: 'blob' }
  if (agentId != null && agentId !== '') config.params = { agent_id: agentId }
  return client.get('/api/agents/template/download', config)
}

// 進階實作範例。與快速骨架是兩套獨立下載，不接受 agent_id。
export const downloadAdvancedExample = () =>
  client.get('/api/agents/examples/advanced/download', { responseType: 'blob' })

// RFC 6266 / 5987。只採用後端給的 filename，沒有就不猜。
// AxiosHeaders 的讀法是 .get()，普通物件則直接取欄位。
export function filenameFromContentDisposition(headers) {
  const raw = typeof headers?.get === 'function'
    ? headers.get('content-disposition')
    : headers?.['content-disposition']
  const value = String(raw || '')
  const star = /filename\*=(?:UTF-8|utf-8)''([^;]+)/.exec(value)
  if (star) {
    try { return decodeURIComponent(star[1].trim()) } catch { /* 壞的編碼當沒有檔名 */ }
  }
  const plain = /filename="([^"]+)"/.exec(value) || /filename=([^;]+)/.exec(value)
  if (!plain) return ''
  return plain[1].trim().replace(/^"(.*)"$/, '$1')
}

// P2.1 — public CSPKI CA bundle for JWKS over https (agent-side trust anchor).
// Backend route required: GET /api/agents/platform-ca/download → application/x-pem-file
// (serves services/csp/app/services/cspki_ca_bundle.pem).
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
