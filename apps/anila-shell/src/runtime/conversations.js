// Wrappers for the CSP control-plane endpoints the UI uses to persist
// conversations, messages, attachments, share links, and handoffs.
//
// All helpers accept an `authRequest(path, options)` callable (typically
// `useAuth().authRequest`) that handles JWT + auto-refresh. Attachment upload
// takes `multipartRequest(path, formData)` instead because the browser must
// set the multipart boundary itself.

// Origin tag for this frontend. Migration 0023 added the
// `conversations.origin` column so multiple SPAs (ANILA UI + ANILALM
// + future bots) can co-exist on the same backend without bleeding
// each other's chat history into each other's sidebars.
export const ANILA_UI_ORIGIN = "anila-ui";

// ── Conversations ───────────────────────────────────────────────────────────

/**
 * List conversations the user owns, EXCLUDING ANILALM (the knowledge-base
 * SPA) ones. NULL-origin rows (legacy / pre-migration) are kept so users
 * don't lose their existing chat history. ``allOrigins=true`` is an
 * escape hatch for an admin debug view; default is what the sidebar wants.
 */
export function listConversations(authRequest, { allOrigins = false } = {}) {
  const qs = allOrigins ? "" : "?exclude_origin=anilalm";
  return authRequest(`/api/conversations${qs}`, { method: "GET" });
}

export function createConversation(authRequest, { title, agentId } = {}) {
  return authRequest("/api/conversations", {
    method: "POST",
    body: JSON.stringify({
      title: title || "新對話",
      agent_id: typeof agentId === "number" ? agentId : null,
      origin: ANILA_UI_ORIGIN,
    }),
  });
}

export function getConversation(authRequest, convId) {
  return authRequest(`/api/conversations/${convId}`, { method: "GET" });
}

export function updateConversationTitle(authRequest, convId, title) {
  return authRequest(`/api/conversations/${convId}`, {
    method: "PUT",
    body: JSON.stringify({ title }),
  });
}

export function deleteConversation(authRequest, convId) {
  return authRequest(`/api/conversations/${convId}`, { method: "DELETE" });
}

export function classifyConversation(authRequest, convId) {
  return authRequest(`/api/conversations/${convId}/classify`, { method: "POST" });
}

/**
 * 匯出收據(W1-1④)。**產檔前**呼叫:回應帶伺服器權威的密等頁首,`ANILA_
 * EXPORT_RECORD_REQUIRED` 為真時同時在 `export_records` 落一列。
 *
 * 為什麼不是產檔後才記:記在後面的話,產檔成功而落列失敗就是一份沒有紀錄的
 * 外流檔案 —— 而那正是本包要修的狀態。呼叫端(`runtime/exportGuard.js`)對
 * 任何失敗一律 fail-closed。
 */
export function recordConversationExport(authRequest, convId, format) {
  return authRequest(`/api/conversations/${convId}/export-record`, {
    method: "POST",
    body: JSON.stringify({ format }),
  });
}

// ── Messages ────────────────────────────────────────────────────────────────

export function appendMessage(authRequest, convId, payload) {
  const body = {
    role: payload.role,
    content: payload.content,
    trace_id: payload.traceId || null,
    latency_ms:
      typeof payload.latencyMs === "number" ? payload.latencyMs : null,
    model_name: payload.modelName || null,
    agent_name: payload.agentName || null,
    metadata: payload.metadata || null,
  };
  return authRequest(`/api/conversations/${convId}/messages`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

// Record thumbs-up/down feedback. rating = "up" | "down" | null (null clears).
export function rateMessage(authRequest, convId, messageId, rating, feedback = null) {
  const payload = { rating };
  if (feedback) {
    if (feedback.comment) payload.comment = feedback.comment;
    if (feedback.reasons) payload.reasons = feedback.reasons;
  }
  return authRequest(`/api/conversations/${convId}/messages/${messageId}/rating`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

// Rewrite a user message's content and drop every message after it.
// The caller must re-trigger a chat turn to produce a new assistant reply.
export function editUserMessage(authRequest, convId, messageId, content) {
  return authRequest(`/api/conversations/${convId}/messages/${messageId}/edit`, {
    method: "PUT",
    body: JSON.stringify({ content }),
  });
}

// In-place patch of an existing message (non-truncating). Used by assistant
// regenerate to replace the old reply without creating a new DB row.
export function updateMessage(authRequest, convId, messageId, patch) {
  const body = {
    content: patch.content ?? null,
    trace_id: patch.traceId ?? null,
    latency_ms:
      typeof patch.latencyMs === "number" ? patch.latencyMs : null,
    model_name: patch.modelName ?? null,
    agent_name: patch.agentName ?? null,
    metadata: patch.metadata ?? null,
  };
  return authRequest(`/api/conversations/${convId}/messages/${messageId}`, {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

// ── Shares ──────────────────────────────────────────────────────────────────

export function listShares(authRequest, convId) {
  return authRequest(`/api/conversations/${convId}/shares`, { method: "GET" });
}

export function createShare(authRequest, convId, { mode = "read_only", allowFork = false, expiresAt = null } = {}) {
  return authRequest(`/api/conversations/${convId}/shares`, {
    method: "POST",
    body: JSON.stringify({
      mode,
      allow_fork: allowFork,
      expires_at: expiresAt,
    }),
  });
}

export function revokeShare(authRequest, convId, shareId) {
  return authRequest(`/api/conversations/${convId}/shares/${shareId}`, {
    method: "DELETE",
  });
}

// Server returns a token; callers build the public URL themselves. Keeping it
// a pure helper so the base can be swapped for dev/prod without touching the
// call sites.
export function buildShareUrl(token, { baseUrl } = {}) {
  const base = baseUrl || (typeof window !== "undefined" ? window.location.origin : "");
  return `${base}/s/c/${token}`;
}

// ── Attachments ─────────────────────────────────────────────────────────────

export function uploadAttachment(multipartRequest, file, { conversationId, messageId } = {}) {
  const form = new FormData();
  form.append("file", file);
  if (typeof conversationId === "number") {
    form.append("conversation_id", String(conversationId));
  }
  if (typeof messageId === "number") {
    form.append("message_id", String(messageId));
  }
  return multipartRequest("/api/attachments", form);
}

// ── Handoffs ────────────────────────────────────────────────────────────────

export function listHandoffs(authRequest) {
  return authRequest("/api/handoffs", { method: "GET" });
}

export function createHandoff(authRequest, { conversationId, toUserId = null, toAgent = null, note = null }) {
  return authRequest("/api/handoffs", {
    method: "POST",
    body: JSON.stringify({
      conversation_id: conversationId,
      to_user_id: toUserId,
      to_agent: toAgent,
      note,
    }),
  });
}

export function acceptHandoff(authRequest, handoffId) {
  return authRequest(`/api/handoffs/${handoffId}/accept`, { method: "POST" });
}

export function rejectHandoff(authRequest, handoffId) {
  return authRequest(`/api/handoffs/${handoffId}/reject`, { method: "POST" });
}

export function cancelHandoff(authRequest, handoffId) {
  return authRequest(`/api/handoffs/${handoffId}/cancel`, { method: "POST" });
}

// Per-agent functions (2026-06-11, extensible). Developer-designed in the
// CSP console; the chat UI renders them for the active agent by kind. agentRef
// is the agent NAME on the data plane (e.g. "image-generator"). Read-only.
export function listAgentFunctions(authRequest, agentRef) {
  return authRequest(`/api/agents/${agentRef}/functions`, { method: "GET" });
}

// Server-synced UI settings (2026-06-12). Per-user folders/stars/tweaks stored
// in CSP (users.ui_settings) instead of browser localStorage, so they follow
// the user across shared PKI-card workstations.
export function getUiSettings(authRequest) {
  return authRequest("/api/users/me/ui-settings", { method: "GET" });
}
export function putUiSettings(authRequest, uiSettings) {
  return authRequest("/api/users/me/ui-settings", {
    method: "PUT",
    body: JSON.stringify({ ui_settings: uiSettings }),
  });
}

// Full-text search over the user's conversations (title + message content,
// server-side ILIKE). Returns conversations with an optional snippet.
export function searchConversations(authRequest, q, { limit = 30 } = {}) {
  const qs = new URLSearchParams({ q, limit: String(limit) }).toString();
  return authRequest(`/api/conversations/search?${qs}`, { method: "GET" });
}

// Admin announcement banners shown at the top of the chat UI.
export function listActiveBanners(authRequest) {
  return authRequest("/api/banners/active", { method: "GET" });
}
