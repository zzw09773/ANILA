// Service-client (Router / worker / admin-tool) credential management.
// Platform-internal s2s only — not the agent dispatch JWT path.

import client from './client'

export async function listServiceClients() {
  const { data } = await client.get('/api/service-clients')
  return data
}

export async function createServiceClient(payload) {
  // payload: { client_name, client_type: "router"|"worker"|"studio"|"admin_tool", description? }
  // File-provisioned clients come back as delivery=file and no service_token.
  const { data } = await client.post('/api/service-clients', payload)
  return data
}

export async function issueStaticForClient(clientId) {
  // Emergency-only path. delivery=emergency_only and the plaintext is shown once.
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
