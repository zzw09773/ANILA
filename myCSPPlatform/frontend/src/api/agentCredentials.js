// Agent credential management — Sprint 8 X / Phase A.
//
// Wraps the seven endpoints CSP exposes for per-agent service token
// lifecycle. The plaintext token (bsk- and csk-) is returned by CSP
// exactly once; callers MUST surface it to the operator on the spot
// because the server only persists a hash / encrypted envelope.

import client from './client'

/**
 * @typedef {Object} CredentialRow
 * @property {number} id
 * @property {number} agent_id
 * @property {string|null} label
 * @property {boolean} is_active
 * @property {boolean} is_legacy
 * @property {string} issued_at
 * @property {string|null} rotated_at
 * @property {string|null} revoked_at
 * @property {boolean} has_previous_token
 * @property {string|null} previous_expires_at
 * @property {string|null} client_cert_fingerprint
 */

/**
 * @typedef {Object} BootstrapIssued
 * @property {string} bootstrap_token   bsk-… (one-shot, 15 min default TTL)
 * @property {string} expires_at
 * @property {number} agent_id
 * @property {string} agent_name
 * @property {string} endpoint_url
 */

/**
 * @typedef {Object} ServiceTokenIssued
 * @property {string} service_token     csk-… (long-lived; one-shot display)
 * @property {number} credential_id
 * @property {string} issued_at
 * @property {string|null} label
 */

export async function listAgentCredentials(agentId) {
  const { data } = await client.get(`/api/agents/${agentId}/credentials`)
  return /** @type {CredentialRow[]} */ (data)
}

export async function issueBootstrapToken(agentId, ttlSeconds = 900) {
  const { data } = await client.post(
    `/api/agents/${agentId}/issue-bootstrap`,
    { ttl_seconds: ttlSeconds },
  )
  return /** @type {BootstrapIssued} */ (data)
}

export async function issueStaticCredential(agentId, label = null) {
  const body = label ? { label } : {}
  const { data } = await client.post(
    `/api/agents/${agentId}/credentials/issue-static`,
    body,
  )
  return /** @type {ServiceTokenIssued} */ (data)
}

export async function rotateAgentCredential(agentId, credentialId, graceSeconds = 86400) {
  const { data } = await client.post(
    `/api/agents/${agentId}/credentials/${credentialId}/rotate`,
    { grace_seconds: graceSeconds },
  )
  return /** @type {ServiceTokenIssued} */ (data)
}

export async function revokeAgentCredential(agentId, credentialId) {
  await client.delete(`/api/agents/${agentId}/credentials/${credentialId}`)
}
