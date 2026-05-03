// Service-client (Router / worker / admin-tool) credential management.
// Sprint 8 X / Phase A. Same shape as agentCredentials.js but talks to
// the /api/service-clients/* family because Routers / workers are not
// agents and don't go through the bootstrap / approval flow.

import client from './client'

export async function listServiceClients() {
  const { data } = await client.get('/api/service-clients')
  return data
}

export async function createServiceClient(payload) {
  // payload: { client_name, client_type: "router"|"worker"|"admin_tool", description? }
  const { data } = await client.post('/api/service-clients', payload)
  // → { service_token, client: {...} } — surface plaintext exactly once.
  return data
}

export async function issueStaticForClient(clientId) {
  const { data } = await client.post(`/api/service-clients/${clientId}/issue-static`, {})
  return data
}

export async function rotateServiceClient(clientId, graceSeconds = 86400) {
  const { data } = await client.post(
    `/api/service-clients/${clientId}/rotate`,
    { grace_seconds: graceSeconds },
  )
  return data
}

export async function revokeServiceClient(clientId) {
  await client.delete(`/api/service-clients/${clientId}`)
}
