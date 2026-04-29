// ANILA Functions v1 — API client.
//
// Talks to CSP /api/functions/*. Reuses authRequestWithRefresh from
// runtime/api.js so JWT refresh + CSRF token handling stays
// consistent with the rest of the SPA. SSE for /run uses a manual
// fetch() with credentials so we can stream Response.body chunks.

import { authRequestWithRefresh } from "./api.js";

async function jsonRequest(path, options = {}, authState, onTokenRefresh, onAuthExpired) {
  const resp = await authRequestWithRefresh(path, options, authState, onTokenRefresh, onAuthExpired);
  if (!resp.ok) {
    let detail = null;
    try { detail = await resp.json(); } catch (_) { /* ignore */ }
    const err = new Error(`Functions API ${resp.status}`);
    err.status = resp.status;
    err.detail = detail;
    throw err;
  }
  if (resp.status === 204) return null;
  return resp.json();
}

// ── Function CRUD ──────────────────────────────────────────────────────

export function listFunctions(filters, ...auth) {
  const qs = new URLSearchParams();
  if (filters?.author) qs.set("author", filters.author);
  if (filters?.status) qs.set("status", filters.status);
  if (filters?.tag)    qs.set("tag", filters.tag);
  if (filters?.q)      qs.set("q", filters.q);
  const path = "/api/functions" + (qs.toString() ? `?${qs}` : "");
  return jsonRequest(path, { method: "GET" }, ...auth);
}

export function createFunction(payload, ...auth) {
  return jsonRequest("/api/functions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  }, ...auth);
}

export function getFunction(slug, ...auth) {
  return jsonRequest(`/api/functions/${encodeURIComponent(slug)}`, { method: "GET" }, ...auth);
}

export function patchFunction(slug, payload, ...auth) {
  return jsonRequest(`/api/functions/${encodeURIComponent(slug)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  }, ...auth);
}

export function deleteFunction(slug, ...auth) {
  return jsonRequest(`/api/functions/${encodeURIComponent(slug)}`, { method: "DELETE" }, ...auth);
}

// ── Versions ───────────────────────────────────────────────────────────

export function saveVersion(slug, payload, ...auth) {
  return jsonRequest(`/api/functions/${encodeURIComponent(slug)}/versions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  }, ...auth);
}

export function listVersions(slug, ...auth) {
  return jsonRequest(`/api/functions/${encodeURIComponent(slug)}/versions`, { method: "GET" }, ...auth);
}

// ── Valves ─────────────────────────────────────────────────────────────

export function getValves(slug, ...auth) {
  return jsonRequest(`/api/functions/${encodeURIComponent(slug)}/valves`, { method: "GET" }, ...auth);
}

export function putValves(slug, values, ...auth) {
  return jsonRequest(`/api/functions/${encodeURIComponent(slug)}/valves`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ values }),
  }, ...auth);
}

// ── Marketplace ────────────────────────────────────────────────────────

export function forkFunction(slug, payload, ...auth) {
  return jsonRequest(`/api/functions/${encodeURIComponent(slug)}/fork`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  }, ...auth);
}

export function reportFunction(slug, reason, ...auth) {
  return jsonRequest(`/api/functions/${encodeURIComponent(slug)}/report`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reason }),
  }, ...auth);
}

export function quarantineFunction(slug, reason, ...auth) {
  return jsonRequest(`/api/functions/${encodeURIComponent(slug)}/quarantine`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reason }),
  }, ...auth);
}

export function unquarantineFunction(slug, ...auth) {
  return jsonRequest(`/api/functions/${encodeURIComponent(slug)}/unquarantine`, { method: "POST" }, ...auth);
}

// ── Run audit ──────────────────────────────────────────────────────────

export function listRuns(slug, ...auth) {
  return jsonRequest(`/api/functions/${encodeURIComponent(slug)}/runs`, { method: "GET" }, ...auth);
}

export function getRun(runId, ...auth) {
  return jsonRequest(`/api/functions/runs/${runId}`, { method: "GET" }, ...auth);
}

// ── Enabled actions (chat toolbar) ─────────────────────────────────────

export function listEnabledActions(...auth) {
  return jsonRequest("/api/functions/enabled-actions", { method: "GET" }, ...auth);
}

// ── Run (SSE) ──────────────────────────────────────────────────────────
//
// Custom fetch + manual SSE chunk parsing because authRequestWithRefresh
// returns a parsed Response but doesn't expose the streaming body the
// way EventSource would. We need credentials:include for the cookies and
// X-CSRF-Token for the mutating POST.

export async function runFunctionStream(slug, payload, csrfToken) {
  const resp = await fetch(`/api/functions/${encodeURIComponent(slug)}/run`, {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(csrfToken ? { "X-CSRF-Token": csrfToken } : {}),
    },
    body: JSON.stringify(payload),
  });
  if (!resp.ok) {
    let detail = null;
    try { detail = await resp.json(); } catch (_) { /* ignore */ }
    const err = new Error(`Function run ${resp.status}`);
    err.status = resp.status;
    err.detail = detail;
    throw err;
  }
  return resp;  // caller consumes resp.body via getReader
}
