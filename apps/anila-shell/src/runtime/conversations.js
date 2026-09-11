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

export function createConversation(authRequest, { title, agentId, routerModelId } = {}) {
  const body = {
    title: title || "新對話",
    agent_id: typeof agentId === "number" ? agentId : null,
    origin: ANILA_UI_ORIGIN,
  };
  if (typeof routerModelId === "number") body.router_model_id = routerModelId;
  return authRequest("/api/conversations", {
    method: "POST",
    body: JSON.stringify(body),
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
// Optional fine score (up→6–10, down→1–5) via feedback.rating_score.
export function rateMessage(authRequest, convId, messageId, rating, feedback = null) {
  const payload = { rating };
  if (feedback) {
    if (feedback.comment) payload.comment = feedback.comment;
    if (feedback.reasons) payload.reasons = feedback.reasons;
    if (Object.prototype.hasOwnProperty.call(feedback, "rating_score")) {
      payload.rating_score = feedback.rating_score;
    }
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

/**
 * Persist the user message AND reserve its assistant row in ONE round trip.
 *
 * 兩次往返之間的 RTT 就是「兩個分頁同一瞬間按 Enter」那個 bug 的窗口:
 * 後到的 append 掛在前一則使用者訊息底下,前一個分頁的 reserve 隨即 409,
 * 那則使用者訊息就永遠拿不到答案。伺服器把兩件事放進同一個交易。
 * 回應是 {user, assistant}。
 */
export function startTurn(authRequest, convId, payload) {
  const body = {
    content: payload.content,
    stream_writer: payload.streamWriter,
    model_name: payload.modelName || null,
    agent_name: payload.agentName || null,
  };
  return authRequest(`/api/conversations/${convId}/turn`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/**
 * Edit-and-re-ask head: branch the user message AND reserve its assistant row.
 *
 * 與 startTurn 是同一件事,差別只在新的使用者訊息是既有那一則的同層兄弟。
 * 分開兩件事做的話,branch 之後 active leaf 會停在一則使用者訊息上,串流
 * 期間插話就會 user → user,編輯後那題的答案再也寫不進去(後端 400)。
 * 回應是 {user, assistant}。
 */
export function branchTurn(authRequest, convId, messageId, payload) {
  const body = {
    content: payload.content,
    stream_writer: payload.streamWriter,
    model_name: payload.modelName || null,
    agent_name: payload.agentName || null,
  };
  return authRequest(
    `/api/conversations/${convId}/messages/${messageId}/branch-turn`,
    { method: "POST", body: JSON.stringify(body) },
  );
}

// POST /messages/{id}/reserve-reply 這個端點仍然存在(start_turn 建在同一個
// 原始操作上),但前端沒有任何呼叫端 —— 送出路徑一律走 startTurn 的一次往返。
// 這裡不留沒有人用的包裝函式。

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
    // 預留列的寫入者權杖;一般 patch 不帶(後端只在該列仍未終局時檢查)。
    stream_writer: patch.streamWriter ?? null,
  };
  const options = {
    method: "PUT",
    body: JSON.stringify(body),
  };
  // 視窗卸載途中送出的「標記為中斷」需要 keepalive,否則瀏覽器會直接
  // 取消這個請求。一般呼叫不帶。
  if (patch.keepalive) options.keepalive = true;
  return authRequest(`/api/conversations/${convId}/messages/${messageId}`, options);
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

/** POST /api/attachments/bind — attach orphans (or already-this-conv files) to a conversation. */
export function bindAttachments(authRequest, { conversationId, referenceIds }) {
  return authRequest("/api/attachments/bind", {
    method: "POST",
    body: JSON.stringify({
      conversation_id: conversationId,
      reference_ids: referenceIds,
    }),
  });
}

/** GET /api/attachments/{reference_id}/meta — includes extract_status. */
export function getAttachmentMeta(authRequest, referenceId) {
  return authRequest(
    `/api/attachments/${encodeURIComponent(referenceId)}/meta`,
    { method: "GET" },
  );
}

/** Terminal extract_status values from CSP (pending is non-terminal). */
export const EXTRACT_TERMINAL_STATUSES = new Set([
  "ok",
  "unsupported",
  "failed",
  "too_large",
]);

/** zh-TW reasons shown on chips / banners (aligned with CSP prompt notices). */
export const EXTRACT_STATUS_REASONS = {
  unsupported: "不支援的檔案格式",
  failed: "解析失敗",
  too_large: "抽取文字超過儲存上限",
};

export function extractStatusReason(status, extractError) {
  if (!EXTRACT_STATUS_REASONS[status]) return null;
  // Per-status policy for extract_error (not a blanket rule):
  // - `failed`: always the fixed zh-TW label. Backend exception text may
  //   contain filesystem paths or module names and must never reach the UI.
  // - `unsupported` / `too_large`: surface the backend's user-facing message
  //   when present (fixed actionable set / storage-cap wording). Fall back
  //   to the generic label when absent or blank.
  if (status === "failed") return EXTRACT_STATUS_REASONS.failed;
  if (
    (status === "unsupported" || status === "too_large")
    && typeof extractError === "string"
    && extractError.trim()
  ) {
    return extractError.trim();
  }
  return EXTRACT_STATUS_REASONS[status];
}

/**
 * Poll attachment meta until extract_status is terminal, or give up ~10s.
 * On timeout / network blips: timedOut=true and status stays pending —
 * caller must NOT treat that as a user-visible failure.
 */
export async function pollAttachmentExtractStatus(
  fetchMeta,
  referenceId,
  {
    // 0.5 + 1 + 2×4 ≈ 9.5s of backoff after each pending read.
    delaysMs = [500, 1000, 2000, 2000, 2000, 2000],
    sleep = (ms) => new Promise((r) => setTimeout(r, ms)),
  } = {},
) {
  for (let i = 0; ; i++) {
    try {
      const meta = await fetchMeta(referenceId);
      const status = meta?.extract_status || "pending";
      if (EXTRACT_TERMINAL_STATUSES.has(status)) {
        return {
          status,
          extractError: meta?.extract_error ?? null,
          timedOut: false,
        };
      }
    } catch {
      // Stay quiet — a blip must not become a false failure banner.
    }
    if (i >= delaysMs.length) {
      return { status: "pending", extractError: null, timedOut: true };
    }
    await sleep(delaysMs[i]);
  }
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

export function listRouterModels(authRequest) {
  return authRequest("/api/router-models", { method: "GET" });
}

export function setConversationRouterModel(authRequest, convId, { routerModelId, expectedVersion } = {}) {
  return authRequest(`/api/conversations/${convId}/router-model`, {
    method: "PUT",
    body: JSON.stringify({
      router_model_id: routerModelId,
      expected_version: expectedVersion ?? 0,
    }),
  });
}
