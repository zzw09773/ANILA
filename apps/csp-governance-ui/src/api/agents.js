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

// P2.1 — 平台 CA。GET /api/agents/platform-ca/download 已存在。
// 503 = 這次部署缺檔（「請聯絡維運」），不是端點沒做。
// 快速起步 zip 已含 ca.pem。
export const downloadPlatformCa = () =>
  client.get('/api/agents/platform-ca/download', { responseType: 'blob' })

// P2.1 — 單檔驗簽。GET /api/agents/anila-verify/download 已存在，
// 檔名 anila_verify.py。503 = 這次部署缺檔（「請聯絡維運」）。
// 快速起步 zip 已含同一支 anila_verify.py。
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
