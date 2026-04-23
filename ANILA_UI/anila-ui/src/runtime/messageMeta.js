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

  // Drop undefined-only results (empty-object meta isn't useful to persist).
  const hasAnyValue = Object.values(base).some(
    (v) => v !== undefined && v !== null && (!Array.isArray(v) || v.length > 0),
  );
  return hasAnyValue ? base : null;
}

// Re-export for tests that want to audit the list of fields we consider
// "cumulative" — kept as a single source of truth for the helper's contract.
export const CUMULATIVE_META_FIELDS = ACCUMULATED_FALLBACKS;
