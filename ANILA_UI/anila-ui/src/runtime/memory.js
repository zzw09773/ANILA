// Wrappers for the per-user memory endpoints (CSP /api/memory/*).
//
// All helpers take the `authRequest(path, options)` callable from
// `useAuth()`. Backend scopes everything to the calling user — no
// admin override path; an admin who needs to inspect another user's
// memory hits the DB directly with audit trail.

export function listFacts(authRequest) {
  return authRequest("/api/memory/facts", { method: "GET" });
}

export function deleteFact(authRequest, factId) {
  return authRequest(`/api/memory/facts/${factId}`, { method: "DELETE" });
}

export function clearFacts(authRequest) {
  return authRequest("/api/memory/facts", { method: "DELETE" });
}

export function listChunks(authRequest, { limit = 50 } = {}) {
  return authRequest(`/api/memory/chunks?limit=${limit}`, { method: "GET" });
}

export function clearChunks(authRequest) {
  return authRequest("/api/memory/chunks", { method: "DELETE" });
}
