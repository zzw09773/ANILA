// OW-1 message-tree helpers (server-truth sibling nav + path merge).
// Pure / unit-testable — no React, no network. See
// docs/plans/ow1-message-tree-blueprint.md §1 Q9 / §2 Create / §5 frontend.

/**
 * Derive ChatGPT-style pager fields from a client message that already
 * carries siblingIndex / siblingCount / siblingIds (mapped from MessageOut).
 */
export function deriveSiblingNav(msg) {
  const siblingCount =
    typeof msg?.siblingCount === "number" && msg.siblingCount > 0
      ? msg.siblingCount
      : 1;
  const siblingIndex =
    typeof msg?.siblingIndex === "number" && msg.siblingIndex >= 0
      ? msg.siblingIndex
      : 0;
  const siblingIds = Array.isArray(msg?.siblingIds) ? msg.siblingIds : [];
  return { siblingIndex, siblingCount, siblingIds };
}

/**
 * Return the sibling message id at siblingIndex+delta, or null at the ends /
 * when sibling_ids is incomplete.
 */
export function neighbourId(msg, delta) {
  const { siblingIndex, siblingIds } = deriveSiblingNav(msg);
  const next = siblingIndex + delta;
  if (next < 0 || next >= siblingIds.length) return null;
  const id = siblingIds[next];
  return typeof id === "number" ? id : null;
}

/**
 * Pager visibility / affordance state. Hidden when there is only one sibling;
 * both directions disabled while streaming; prev disabled at index 0.
 */
export function pagerState(msg, { streaming = false } = {}) {
  const { siblingIndex, siblingCount } = deriveSiblingNav(msg);
  const visible = siblingCount > 1;
  return {
    visible,
    canPrev: visible && siblingIndex > 0 && !streaming,
    canNext: visible && siblingIndex < siblingCount - 1 && !streaming,
    label: `${siblingIndex + 1} / ${siblingCount}`,
    siblingIndex,
    siblingCount,
  };
}

/**
 * A branch exists only when a message has more than one sibling. `parentId`
 * says nothing about branching — every message after the first has one — so
 * gating branch affordances on it lit up a "delete this branch" control on
 * perfectly healthy first exchanges. Single predicate, single place.
 */
export function hasBranch(msg) {
  return deriveSiblingNav(msg).siblingCount > 1;
}

/**
 * Reconcile the conversation's message list with a server active path.
 *
 * Server-known messages (numeric dbId) are server truth: anything missing
 * from the path — a legitimately deleted branch, an inactive branch — is
 * dropped. Local-only entries (no dbId) have NO server counterpart to compare
 * against, so a wholesale replace erased them: the hydrate GET fired when a
 * brand-new conversation is selected returns an empty path and used to land
 * after the optimistic user+assistant bubbles were appended, wiping them.
 * That is why the first click on a suggestion chip looked like a no-op.
 * Keeping only dbId-less entries cannot resurrect a deleted message, because
 * anything the server ever stored carries a dbId.
 *
 * Preserves client-only fields (piiHits, explicitAgents, finishReason,
 * routedAgentId) by dbId.
 *
 * `serverMapped` must already be in the client message shape (dbId, role, …);
 * this helper only merges + stamps conversationId.
 */
export function applyServerPath(prevList, serverMapped, convId) {
  const clientOnly = new Map();
  for (const m of prevList || []) {
    if (typeof m?.dbId === "number") {
      clientOnly.set(m.dbId, {
        piiHits: m.piiHits,
        explicitAgents: m.explicitAgents,
        finishReason: m.finishReason,
        routedAgentId: m.routedAgentId,
      });
    }
  }
  const fromServer = (serverMapped || []).map((sm) => {
    const preserved = clientOnly.get(sm.dbId) || {};
    const next = { ...sm, conversationId: convId };
    if (preserved.piiHits !== undefined) next.piiHits = preserved.piiHits;
    if (preserved.explicitAgents !== undefined) {
      next.explicitAgents = preserved.explicitAgents;
    }
    if (preserved.finishReason !== undefined) {
      next.finishReason = preserved.finishReason;
    }
    if (preserved.routedAgentId !== undefined) {
      next.routedAgentId = preserved.routedAgentId;
    }
    return next;
  });
  // Pending / unpersisted tail: the server cannot have an opinion on these.
  const pending = (prevList || [])
    .filter((m) => m && typeof m.dbId !== "number")
    .map((m) => (m.conversationId === convId ? m : { ...m, conversationId: convId }));
  return pending.length > 0 ? [...fromServer, ...pending] : fromServer;
}

/**
 * Persist the user turn before streaming. On failure for a numeric convId the
 * caller MUST abort (no stream, no assistant persist). Non-numeric convId is
 * the degraded offline path — proceed without a parent_id.
 */
export async function tryPersistUserMessage({
  appendMessage,
  authRequest,
  convId,
  content,
}) {
  if (typeof convId !== "number") {
    return { aborted: false, offline: true, userDbId: null, saved: null };
  }
  try {
    const saved = await appendMessage(authRequest, convId, {
      role: "user",
      content,
    });
    if (saved && typeof saved.id === "number") {
      return { aborted: false, offline: false, userDbId: saved.id, saved };
    }
    return {
      aborted: true,
      offline: false,
      userDbId: null,
      saved: null,
      error: new Error("使用者訊息儲存失敗"),
    };
  } catch (error) {
    return {
      aborted: true,
      offline: false,
      userDbId: null,
      saved: null,
      error,
    };
  }
}

/**
 * Persist the assistant turn. NEVER silent: both a rejected POST (e.g. the
 * backend refusing an explicit parent role with 400) and a 2xx body without
 * an id mean the answer on screen is not in the conversation record, and the
 * user must be told — otherwise the reply simply vanishes on the next reload.
 * Returns a user-facing `notice` the caller pins to the assistant bubble.
 */
export async function persistAssistantTurn({
  appendMessage,
  authRequest,
  convId,
  payload,
}) {
  const notice = (reason) =>
    `這則回答沒有存進對話紀錄（${reason}），重新整理後就會消失。請先複製內容，或重新產生一次。`;
  try {
    const saved = await appendMessage(authRequest, convId, payload);
    if (saved && typeof saved.id === "number") {
      return { ok: true, saved, error: null, notice: null };
    }
    const error = new Error("對話訊息儲存失敗");
    return { ok: false, saved: null, error, notice: notice(error.message) };
  } catch (error) {
    const reason = error?.message || "對話訊息儲存失敗";
    return { ok: false, saved: null, error, notice: notice(reason) };
  }
}

/**
 * Orchestrate user-persist → stream → assistant-persist. When user persist
 * aborts, stream and appendAssistant are never called.
 */
export async function runPersistedUserTurn({
  appendMessage,
  authRequest,
  convId,
  content,
  stream,
  appendAssistant,
}) {
  const gate = await tryPersistUserMessage({
    appendMessage,
    authRequest,
    convId,
    content,
  });
  if (gate.aborted) {
    return { ...gate, streamed: false, assistantPersisted: false };
  }
  const streamResult = await stream(gate);
  if (appendAssistant) {
    await appendAssistant({ ...gate, streamResult });
  }
  return {
    ...gate,
    streamed: true,
    assistantPersisted: typeof appendAssistant === "function",
    streamResult,
  };
}

/**
 * Clear in-flight / placeholder-only flags before restoring a captured list.
 * A snapshot taken around stopStreaming can still carry streaming:true if the
 * aborted turn's catch cleared the flag after capture — restoring that raw
 * list would leave conversationStreaming stuck until reload.
 */
export function sanitizeRestoredMessages(list) {
  return (list || []).map((m) => ({
    ...m,
    streaming: false,
  }));
}

/**
 * Regenerate stream phase: on failure, return the pre-regenerate list so the
 * caller can restore the previous answer (placeholder must not stick).
 */
export async function runRegenerateStreamPhase({ stream, preList }) {
  try {
    await stream();
    return { ok: true, messages: null, error: null };
  } catch (error) {
    return { ok: false, messages: sanitizeRestoredMessages(preList), error };
  }
}

/**
 * PUT /active-leaf then rebuild the local list from the response.
 * Never mutates `prevList` (no optimistic path rewrite).
 */
export async function switchBranch({
  setActiveLeaf,
  authRequest,
  convId,
  messageId,
  prevList,
  mapServerMessage,
}) {
  const path = await setActiveLeaf(authRequest, convId, messageId);
  const mapped = (path.messages || []).map((m) => ({
    ...mapServerMessage(m),
    conversationId: convId,
  }));
  return {
    activeLeafMessageId: path.active_leaf_message_id ?? null,
    messages: applyServerPath(prevList, mapped, convId),
  };
}

/**
 * Persist a regenerated assistant reply as a sibling branch.
 * Intentionally calls only branchMessage — never updateMessage / in-place PUT.
 */
export async function persistRegeneratedAssistant({
  branchMessage,
  authRequest,
  convId,
  targetMessageId,
  content,
  traceId,
  latencyMs,
  agentName,
  metadata,
  setActive = true,
}) {
  return branchMessage(authRequest, convId, targetMessageId, {
    role: "assistant",
    content,
    traceId,
    latencyMs,
    agentName,
    metadata,
    setActive,
  });
}
