// Persistence helpers for assistant-message metadata.
//
// SSE emits `anila.trace` steps incrementally and the closing `anila.meta`
// intentionally ships `trace: []` to avoid duplicating those steps. The UI
// accumulates trace (+ reasoning, handoff_chain) into the message's React
// state while streaming. When we persist the message via `/messages`, we
// must merge that accumulated state back into the metadata blob — otherwise
// on reload `messages.metadata_.trace` is empty and the routing trace
// disappears from the conversation.

const ACCUMULATED_FALLBACKS = [
  "trace",
  "reasoning",
  "handoff_chain",
  "citations",
  "follow_ups",
  // Sprint 13 PR B1
  "todos",
  "tool_calls",
  "interrupt",
  "spans",
];

/**
 * Build the metadata payload for a persisted assistant message, merging the
 * final `anila.meta` with values accumulated on the message's React state
 * during streaming. The React state wins for fields that stream cumulatively
 * (trace, reasoning) because the final meta deliberately omits them.
 *
 * @param {object | null | undefined} finalMeta - the last `anila.meta` frame
 * @param {object | null | undefined} messageState - the UI message row
 * @returns {object | null} merged meta, or null when nothing worth persisting
 */
export function buildPersistMeta(finalMeta, messageState) {
  const base = finalMeta && typeof finalMeta === "object" ? { ...finalMeta } : {};
  const state = messageState && typeof messageState === "object" ? messageState : {};

  // trace: prefer the streaming-accumulated list when final meta's trace is
  // missing or empty (the documented case). Non-stream paths that ship a
  // populated trace in the final frame keep that authoritative copy.
  const baseTrace = Array.isArray(base.trace) ? base.trace : [];
  const stateTrace = Array.isArray(state.trace) ? state.trace : [];
  if (baseTrace.length === 0 && stateTrace.length > 0) {
    base.trace = stateTrace;
  } else if (!Array.isArray(base.trace)) {
    base.trace = [];
  }

  // reasoning: streaming delivers deltas via `anila.reasoning`; final meta
  // rarely carries the full string.
  if (
    (base.reasoning == null || base.reasoning === "") &&
    typeof state.reasoning === "string" &&
    state.reasoning.length > 0
  ) {
    base.reasoning = state.reasoning;
  }

  // handoff_chain / citations / follow_ups: final meta usually carries these;
  // fall back to message state only if the final meta omitted them.
  if (!Array.isArray(base.handoff_chain) && Array.isArray(state.handoffChain)) {
    base.handoff_chain = state.handoffChain;
  }
  if (!Array.isArray(base.citations) && Array.isArray(state.citations)) {
    base.citations = state.citations;
  }
  if (!Array.isArray(base.follow_ups) && Array.isArray(state.followUps)) {
    base.follow_ups = state.followUps;
  }

  // classified latch: once-true-forever. A message with classified=true in
  // either source must persist classified=true so reloads still render the
  // lock icon. Never downgrade.
  if (state.classified === true || base.classified === true) {
    base.classified = true;
  }

  // Sprint 13 PR B1: persist Sprint 9-12 typed event state so reloading
  // a conversation rebuilds the same UI affordances.
  //   * todos / tool_calls / spans : streaming-cumulative; final meta
  //     never carries them, take from state.
  //   * interrupt : the most recent INTERRUPT_REQUESTED payload, kept
  //     even after a successful resume so the reload UI can render
  //     "Paused on X / Resumed at Y" affordances.
  if (!Array.isArray(base.todos) && Array.isArray(state.todos)) {
    base.todos = state.todos;
  }
  if (!Array.isArray(base.tool_calls) && Array.isArray(state.toolCalls)) {
    base.tool_calls = state.toolCalls;
  }
  if (!Array.isArray(base.spans) && Array.isArray(state.spans)) {
    base.spans = state.spans;
  }
  if (
    base.interrupt == null &&
    state.interrupt != null &&
    typeof state.interrupt === "object"
  ) {
    base.interrupt = state.interrupt;
  }

  // Drop undefined-only results (empty-object meta isn't useful to persist).
  const hasAnyValue = Object.values(base).some(
    (v) => v !== undefined && v !== null && (!Array.isArray(v) || v.length > 0),
  );
  return hasAnyValue ? base : null;
}

// Re-export for tests that want to audit the list of fields we consider
// "cumulative" — kept as a single source of truth for the helper's contract.
export const CUMULATIVE_META_FIELDS = ACCUMULATED_FALLBACKS;

// The hop the router writes for itself.
const ROUTER_HOP_AGENT_ID = "anila-router";
// ...whose output_summary is the ONLY place the dispatched agent id appears.
// See anila_core/api/router_server.py `_merge_anila_meta`: it prepends
// {agent_id: "anila-router", output_summary: "dispatch to <agent_id>"} and a
// plain downstream agent contributes no hop of its own — so both the first
// and the last element of handoff_chain read "anila-router".
const DISPATCH_SUMMARY = /^\s*dispatch to\s+(\S.*?)\s*$/;

/**
 * Which agent actually produced this answer, per the response metadata.
 *
 * Callers pass the target they were AIMING at as the fallback. On the router
 * path that target is the router itself, so persisting it attributed every
 * answer — and therefore every per-agent feedback row — to "ANILA Router"
 * instead of the agent that did the work.
 *
 * Returns null when the metadata shows no dispatch at all: the router
 * answering directly really was answered by the router, and the caller's
 * existing value is correct.
 *
 * @param {object | null | undefined} meta - an `anila.meta` payload
 * @returns {string | null} the dispatched agent id, or null
 */
export function resolveAnsweringAgentId(meta) {
  // Preferred: the first-class field the router now emits on both the
  // streaming and non-streaming dispatch paths (`_merge_anila_meta`). The
  // prose parse below stays as the fallback for messages persisted before
  // the field existed, and for any upstream that only ships handoff_chain.
  const firstClass = meta?.answering_agent_id;
  if (typeof firstClass === "string" && firstClass.trim()) {
    return firstClass.trim();
  }
  const chain = Array.isArray(meta?.handoff_chain) ? meta.handoff_chain : [];
  // A downstream hop that names itself is the most precise answer, and the
  // LAST such hop is the one that produced the text (chains can nest).
  for (let i = chain.length - 1; i >= 0; i -= 1) {
    const id = chain[i]?.agent_id;
    if (typeof id === "string" && id.trim() && id !== ROUTER_HOP_AGENT_ID) {
      return id.trim();
    }
  }
  // Otherwise fall back to the router's own dispatch record.
  for (let i = chain.length - 1; i >= 0; i -= 1) {
    const match = DISPATCH_SUMMARY.exec(String(chain[i]?.output_summary ?? ""));
    if (match) return match[1];
  }
  return null;
}

/**
 * The `agent_name` to persist on an assistant message: the agent that
 * actually answered, resolved to its display name via the loaded agent list.
 * Falls back to the aimed-at target when the metadata records no dispatch.
 *
 * @param {object | null | undefined} finalMeta - the closing `anila.meta`
 * @param {string | number} effectiveTarget - the agent id the client aimed at
 * @param {Array<{ id?: string | number, name?: string }>} agents
 * @returns {string}
 */
export function resolveAgentNameForPersist(finalMeta, effectiveTarget, agents = []) {
  const list = Array.isArray(agents) ? agents : [];
  const answeringId = resolveAnsweringAgentId(finalMeta);
  if (answeringId) {
    return list.find((a) => a?.id === answeringId)?.name || answeringId;
  }
  return (
    list.find((a) => a?.id === effectiveTarget)?.name || String(effectiveTarget)
  );
}
