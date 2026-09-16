// Compact boundary：把 Router 摘要對回 CSP 訊息 id，並決定下一輪要送哪些 messages。

export const COMPACT_SUMMARY_PREFIX = "[歷史摘要]\n";

export function isInjectedCompactSystem(message) {
  return (
    message?.role === "system"
    && typeof message.content === "string"
    && message.content.startsWith(COMPACT_SUMMARY_PREFIX)
  );
}

export function compactStateFromConv(conv) {
  if (!conv?.compactSummary || conv.compactBoundaryMessageId == null) return null;
  return {
    summary: conv.compactSummary,
    boundaryMessageId: conv.compactBoundaryMessageId,
  };
}

export function compactFieldsFromServer(row) {
  if (
    !row
    || (
      row.compact_summary === undefined
      && row.compact_boundary_message_id === undefined
      && row.compact_updated_at === undefined
    )
  ) {
    return {};
  }
  return {
    compactSummary: row.compact_summary ?? null,
    compactBoundaryMessageId: row.compact_boundary_message_id ?? null,
    compactUpdatedAt: row.compact_updated_at ?? null,
  };
}

export function compactBoundaryOnPath(msgs, boundaryMessageId) {
  if (boundaryMessageId == null) return false;
  return (msgs || []).some((m) => m && m.dbId === boundaryMessageId);
}

/**
 * `kept_from_index` 是**送出的 payload messages** 的索引。
 * 若第一則是前端自加的 `[歷史摘要]` system，先減 1 再對回 sources。
 */
export function resolveKeptBoundary(payloadMessages, keptFromIndex, sources) {
  const offset = isInjectedCompactSystem(payloadMessages?.[0]) ? 1 : 0;
  const idx = Number(keptFromIndex) - offset;
  if (!Number.isInteger(idx) || idx < 0) return null;
  return sources?.[idx] ?? null;
}

/**
 * 邊界訊息若還沒有 dbId（這一輪還在串流／尚未落庫），
 * 取它前一則已 persist 的下一則；都沒有就回 null，呼叫端再等 persist。
 */
export function resolveBoundaryDbId(boundaryMsg, pathMsgs) {
  if (typeof boundaryMsg?.dbId === "number") return boundaryMsg.dbId;
  const list = Array.isArray(pathMsgs) ? pathMsgs : [];
  const pos = list.findIndex((m) => (
    m && (m === boundaryMsg || (boundaryMsg?.id != null && m.id === boundaryMsg.id))
  ));
  const searchEnd = pos >= 0 ? pos : list.length;
  let lastPersisted = -1;
  for (let i = 0; i < searchEnd; i += 1) {
    if (typeof list[i]?.dbId === "number") lastPersisted = i;
  }
  const next = list[lastPersisted + 1];
  return typeof next?.dbId === "number" ? next.dbId : null;
}

export function keptMessageCount(payloadLength, keptFromIndex) {
  const kept = Number(keptFromIndex);
  if (!Number.isFinite(kept)) return 0;
  return Math.max(0, Number(payloadLength) - kept);
}

export const COMPACT_PUT_MAX_ATTEMPTS = 3;

/** pending 以 convId 為 key；值內再存 convId，flush 時必須對得上才准寫。 */
export function readPendingCompact(store, convId) {
  const pending = store.get(convId);
  if (!pending) return null;
  if (pending.convId !== convId) return null;
  return pending;
}

export function writePendingCompact(store, convId, { summary, clientId, attempts } = {}) {
  const prev = store.get(convId);
  store.set(convId, {
    convId,
    summary: summary ?? prev?.summary,
    clientId: clientId !== undefined ? clientId : (prev?.clientId ?? null),
    attempts: attempts ?? prev?.attempts ?? 0,
  });
}

export function compactPutFailureState(pending, convId, summary, clientId) {
  const attempts = (pending?.attempts ?? 0) + 1;
  return {
    abandoned: attempts >= COMPACT_PUT_MAX_ATTEMPTS,
    pending: {
      convId,
      summary,
      clientId: clientId ?? pending?.clientId ?? null,
      attempts,
    },
  };
}
