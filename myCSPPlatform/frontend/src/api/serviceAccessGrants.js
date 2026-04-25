import client from './client'

// Phase 1 §7.5.3 — admin grant CRUD wrapper. The backend endpoints already
// enforce admin role + the XOR (user_id | department_id) shape; this layer
// exists so the Vue views stay declarative.

export const listGrants = (params) =>
  client.get('/api/service-access-grants', { params })

// payload = { platform_link_id, user_id }  OR  { platform_link_id, department_id }
// Both fields together is rejected at the API boundary.
export const createGrant = (payload) =>
  client.post('/api/service-access-grants', payload)

// Soft-revoke (sets revoked_at). Idempotent — calling on an already-revoked
// grant returns the existing revoked_at timestamp without raising.
export const revokeGrant = (grantId) =>
  client.delete(`/api/service-access-grants/${grantId}`)
