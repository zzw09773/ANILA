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

/**
 * Promote a compare-mode answer into a persisted conversation + message tree.
 * Server latches classification from the resolved agent; client must not invent it.
 */
export function adoptConversation(
  authRequest,
  {
    title,
    agentName,
    agentId,
    userContent,
    assistantContent,
    assistantMetadata,
    assistantTraceId,
    assistantLatencyMs,
    assistantAgentName,
  } = {},
) {
  const body = {
    title: title || "採用比較結果",
    origin: ANILA_UI_ORIGIN,
    user_content: userContent,
    assistant_content: assistantContent,
  };
  if (typeof agentName === "string" && agentName) body.agent_name = agentName;
  if (typeof agentId === "number") body.agent_id = agentId;
  if (assistantMetadata && typeof assistantMetadata === "object") {
    body.assistant_metadata = assistantMetadata;
  }
  if (assistantTraceId) body.assistant_trace_id = assistantTraceId;
  if (typeof assistantLatencyMs === "number") {
    body.assistant_latency_ms = assistantLatencyMs;
  }
  if (assistantAgentName) body.assistant_agent_name = assistantAgentName;
  return authRequest("/api/conversations/adopt", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** @param {{ view?: "active" | "all" }} [opts] */
export function getConversation(authRequest, convId, { view } = {}) {
  const qs = view ? `?view=${encodeURIComponent(view)}` : "";
  return authRequest(`/api/conversations/${convId}${qs}`, { method: "GET" });
}

export function updateConversationTitle(authRequest, convId, title) {
  return updateConversation(authRequest, convId, { title });
}

/**
 * Partial update: title and/or the caller's personal meta (starred / folder / tags).
 * User tags must not include the derived ``classified`` tag — the server strips it.
 */
export function updateConversation(authRequest, convId, patch = {}) {
  const body = {};
  if (typeof patch.title === "string") body.title = patch.title;
  if (typeof patch.starred === "boolean") body.starred = patch.starred;
  if (typeof patch.folder === "string") body.folder = patch.folder;
  if (Array.isArray(patch.tags)) {
    body.tags = patch.tags.filter((t) => typeof t === "string" && t !== "classified");
  }
  return authRequest(`/api/conversations/${convId}`, {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

export function deleteConversation(authRequest, convId) {
  return authRequest(`/api/conversations/${convId}`, { method: "DELETE" });
}

export function classifyConversation(authRequest, convId) {
  return authRequest(`/api/conversations/${convId}/classify`, { method: "POST" });
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
  // OW-1: optional parent_id / set_active (blueprint §4).
  if (payload.parentId !== undefined) body.parent_id = payload.parentId;
  if (payload.setActive !== undefined) body.set_active = payload.setActive;
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

/**
 * Create a sibling of ``messageId`` (edit-re-ask OR regenerate).
 * Server sets parent_id = target.parent_id and advances active leaf.
 */
export function branchMessage(authRequest, convId, messageId, payload) {
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
  if (payload.setActive !== undefined) body.set_active = payload.setActive;
  return authRequest(
    `/api/conversations/${convId}/messages/${messageId}/branch`,
    {
      method: "POST",
      body: JSON.stringify(body),
    },
  );
}

/** Switch the active path; response is ConversationPathOut. */
export function setActiveLeaf(authRequest, convId, messageId) {
  return authRequest(`/api/conversations/${convId}/active-leaf`, {
    method: "PUT",
    body: JSON.stringify({ message_id: messageId }),
  });
}

/** Subtree delete; response is ConversationPathOut (repointed active path). */
export function deleteMessageBranch(authRequest, convId, messageId) {
  return authRequest(`/api/conversations/${convId}/messages/${messageId}`, {
    method: "DELETE",
  });
}

// In-place patch of an existing message (non-truncating). Kept for metadata
// patches; ANILA regenerate now uses branchMessage (OW-1) instead.
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

// ── Shares (P4.3 named person / unit; anonymous link retired) ────────────────

export function listShares(authRequest, convId) {
  return authRequest(`/api/conversations/${convId}/shares`, { method: "GET" });
}

export function createShare(
  authRequest,
  convId,
  {
    targetUsername = null,
    targetUserId = null,
    targetDepartmentId = null,
    targetDepartmentName = null,
    mode = "read_only",
    allowFork = false,
    expiresAt = null,
  } = {},
) {
  const body = {
    mode,
    allow_fork: allowFork,
    expires_at: expiresAt,
  };
  if (targetUserId != null) body.target_user_id = targetUserId;
  if (targetUsername) body.target_username = targetUsername;
  if (targetDepartmentId != null) body.target_department_id = targetDepartmentId;
  if (targetDepartmentName) body.target_department_name = targetDepartmentName;
  return authRequest(`/api/conversations/${convId}/shares`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function revokeShare(authRequest, convId, shareId) {
  return authRequest(`/api/conversations/${convId}/shares/${shareId}`, {
    method: "DELETE",
  });
}

/** Human-readable label for a named share row (owner UI). */
export function formatShareTarget(share) {
  if (!share) return "—";
  if (share.target_username || share.target_user_id) {
    return share.target_username
      ? `帳號 ${share.target_username}`
      : `使用者 #${share.target_user_id}`;
  }
  if (share.target_department_name || share.target_department_id) {
    return share.target_department_name
      ? `單位 ${share.target_department_name}`
      : `單位 #${share.target_department_id}`;
  }
  return "（未指定對象）";
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

// `GET /api/handoffs` 同時回「我送出的」與「別人交給我的」。收件匣只該顯示
// 後者、而且還沒處理的 —— 混進自己送出的那些，使用者會對自己的請求按「接受」。
export function incomingPendingHandoffs(rows, currentUserId) {
  if (!Array.isArray(rows) || typeof currentUserId !== "number") return [];
  return rows.filter(
    (h) => h && h.status === "pending" && h.to_user_id === currentUserId,
  );
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
