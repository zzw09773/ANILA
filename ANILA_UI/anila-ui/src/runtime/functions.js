// ANILA Functions v1 — API client.
//
// Talks to CSP /api/functions/*. Cookie-based auth (httpOnly session +
// CSRF echo) — same surface as runtime/api.js authRequestWithRefresh
// but inline here so the Functions feature has a single small dep
// surface and can run from any auth context (Login wrapper, admin
// page, ChatRuntime).
//
// SSE for /run uses the streaming `runFunctionStream` exit which
// returns the raw Response so callers can pipe `response.body` into
// runtime/functionEvents.js's parser.

import { readCsrfCookie } from "./api.js";

const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

async function jsonRequest(path, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  const headers = new Headers(options.headers || {});
  if (!SAFE_METHODS.has(method)) {
    const csrf = readCsrfCookie();
    if (csrf) headers.set("X-CSRF-Token", csrf);
  }
  const resp = await fetch(path, {
    ...options,
    method,
    credentials: "include",
    headers,
  });
  if (!resp.ok) {
    let detail = null;
    try { detail = await resp.json(); } catch (_) { /* ignore */ }
    const err = new Error(`Functions API ${resp.status}`);
    err.status = resp.status;
    err.detail = detail;
    throw err;
  }
  if (resp.status === 204) return null;
  const ct = resp.headers.get("content-type") || "";
  return ct.includes("json") ? resp.json() : resp.text();
}

// ── Function CRUD ──────────────────────────────────────────────────────

export function listFunctions(filters) {
  const qs = new URLSearchParams();
  if (filters?.author) qs.set("author", filters.author);
  if (filters?.status) qs.set("status", filters.status);
  if (filters?.tag)    qs.set("tag", filters.tag);
  if (filters?.q)      qs.set("q", filters.q);
  const path = "/api/functions" + (qs.toString() ? `?${qs}` : "");
  return jsonRequest(path);
}

export function createFunction(payload) {
  return jsonRequest("/api/functions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function getFunction(slug) {
  return jsonRequest(`/api/functions/${encodeURIComponent(slug)}`);
}

export function patchFunction(slug, payload) {
  return jsonRequest(`/api/functions/${encodeURIComponent(slug)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function deleteFunction(slug) {
  return jsonRequest(`/api/functions/${encodeURIComponent(slug)}`, { method: "DELETE" });
}

// ── Versions ───────────────────────────────────────────────────────────

export function saveVersion(slug, payload) {
  return jsonRequest(`/api/functions/${encodeURIComponent(slug)}/versions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function listVersions(slug) {
  return jsonRequest(`/api/functions/${encodeURIComponent(slug)}/versions`);
}

// ── Valves ─────────────────────────────────────────────────────────────

export function getValves(slug) {
  return jsonRequest(`/api/functions/${encodeURIComponent(slug)}/valves`);
}

export function putValves(slug, values) {
  return jsonRequest(`/api/functions/${encodeURIComponent(slug)}/valves`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ values }),
  });
}

// ── Marketplace ────────────────────────────────────────────────────────

export function forkFunction(slug, payload) {
  return jsonRequest(`/api/functions/${encodeURIComponent(slug)}/fork`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  });
}

export function reportFunction(slug, reason) {
  return jsonRequest(`/api/functions/${encodeURIComponent(slug)}/report`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reason }),
  });
}

export function quarantineFunction(slug, reason) {
  return jsonRequest(`/api/functions/${encodeURIComponent(slug)}/quarantine`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reason }),
  });
}

export function unquarantineFunction(slug) {
  return jsonRequest(`/api/functions/${encodeURIComponent(slug)}/unquarantine`, { method: "POST" });
}

// ── Run audit ──────────────────────────────────────────────────────────

export function listRuns(slug) {
  return jsonRequest(`/api/functions/${encodeURIComponent(slug)}/runs`);
}

export function getRun(runId) {
  return jsonRequest(`/api/functions/runs/${runId}`);
}

// ── Enabled actions (chat toolbar) ─────────────────────────────────────

export function listEnabledActions() {
  return jsonRequest("/api/functions/enabled-actions");
}

// ── Run (SSE) ──────────────────────────────────────────────────────────

export async function runFunctionStream(slug, payload, csrfToken) {
  const csrf = csrfToken || readCsrfCookie();
  const resp = await fetch(`/api/functions/${encodeURIComponent(slug)}/run`, {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(csrf ? { "X-CSRF-Token": csrf } : {}),
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
  return resp;
}
