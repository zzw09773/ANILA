// Active-path / sibling helpers for W2-3 message trees (C3 §d).
// Server messages use `id` / `parent_id`; UI messages use `dbId` / `parentId`.
//
// Two different questions used to share the single `dbId` field, which is how
// a row ended up claiming a server id that belonged to a *different* revision:
//
//   `dbId`         — "which server row is the revision I am **showing**?"
//                    `undefined` when that revision was never persisted.
//                    Sole key for rating / update / active-leaf.
//   `forkAnchorId` — "which server row should a regenerate **branch from**?"
//                    A fork creates a sibling under the same parent, so *any*
//                    persisted sibling of the set answers this equally well.
//                    Sticky: it survives switching onto an unpersisted
//                    revision, which is exactly when `dbId` must be absent.
//
// Never collapse them back into one field: an unpersisted revision then
// inherits the outgoing sibling's id, and the UI shows revision A's text while
// the row claims revision B's server id.

function msgId(m) {
  return m?.dbId ?? m?.id;
}

/** A numeric `dbId`, or `undefined` — `null`/absent both mean "not persisted". */
function persistedId(value) {
  return typeof value === "number" ? value : undefined;
}

/**
 * Fork source for a revision set: any persisted sibling. Falls back to the
 * row's own ids so a set that has not been re-derived from the tree still
 * knows where to branch from.
 */
function forkAnchorFor(revisions, row) {
  for (const r of revisions || []) {
    const id = persistedId(r?.dbId);
    if (id !== undefined) return id;
  }
  return persistedId(row?.dbId) ?? persistedId(row?.forkAnchorId);
}

function parentKeyOf(m) {
  return m?.parentId ?? m?.parent_id ?? null;
}

/**
 * Walk parent links from the active leaf back to the root, then reverse.
 * `messages` is the full tree (from `?tree=1`).
 */
export function activePathFromTree(messages, activeLeafId) {
  const byId = new Map();
  for (const m of messages || []) {
    const id = msgId(m);
    if (id != null) byId.set(id, m);
  }
  if (activeLeafId == null) {
    // NULL leaf = newest leaf among nodes with no children.
    const parents = new Set();
    for (const m of byId.values()) {
      const pid = parentKeyOf(m);
      if (pid != null) parents.add(pid);
    }
    let newest = null;
    for (const m of byId.values()) {
      const id = msgId(m);
      if (parents.has(id)) continue;
      if (
        !newest ||
        String(m.createdAt || m.created_at || "") >
          String(newest.createdAt || newest.created_at || "") ||
        id > msgId(newest)
      ) {
        newest = m;
      }
    }
    if (!newest) return [];
    activeLeafId = msgId(newest);
  }
  const chain = [];
  const seen = new Set();
  let cur = byId.get(activeLeafId);
  while (cur && !seen.has(msgId(cur))) {
    const id = msgId(cur);
    seen.add(id);
    chain.push(cur);
    const pid = parentKeyOf(cur);
    cur = pid != null ? byId.get(pid) : null;
  }
  chain.reverse();
  return chain;
}

/** Sibling set sharing the same parent (includes `message`). Sorted by id. */
export function siblingsOf(messages, message) {
  const parentKey = parentKeyOf(message);
  const list = (messages || []).filter((m) => {
    return parentKeyOf(m) === parentKey && (m.role || "") === (message.role || "");
  });
  return list.slice().sort((a, b) => msgId(a) - msgId(b));
}

/**
 * Newest leaf under ``rootId`` (inclusive). When the node is already a leaf,
 * returns it. Multiple descendant leaves → newest by createdAt/id.
 */
export function deepestLeafUnder(messages, rootId) {
  const byId = new Map();
  const children = new Map();
  for (const m of messages || []) {
    const id = msgId(m);
    if (id == null) continue;
    byId.set(id, m);
    const pid = parentKeyOf(m);
    if (pid != null) {
      if (!children.has(pid)) children.set(pid, []);
      children.get(pid).push(m);
    }
  }
  if (!byId.has(rootId)) return rootId;

  const subtree = new Set();
  const stack = [rootId];
  while (stack.length) {
    const id = stack.pop();
    if (subtree.has(id)) continue;
    subtree.add(id);
    for (const child of children.get(id) || []) {
      stack.push(msgId(child));
    }
  }

  let newest = null;
  for (const id of subtree) {
    if ((children.get(id) || []).length > 0) continue;
    const m = byId.get(id);
    if (
      !newest ||
      String(m.createdAt || m.created_at || "") >
        String(newest.createdAt || newest.created_at || "") ||
      id > msgId(newest)
    ) {
      newest = m;
    }
  }
  return newest ? msgId(newest) : rootId;
}

/**
 * Descendant chain under ``rootId`` excluding the root itself, walking from
 * the deepest leaf back up. Empty when ``rootId`` is already a leaf.
 */
export function descendantTailFromTree(messages, rootId) {
  const leafId = deepestLeafUnder(messages, rootId);
  if (leafId === rootId) return [];
  const path = activePathFromTree(messages, leafId);
  const idx = path.findIndex((m) => msgId(m) === rootId);
  if (idx < 0) return [];
  return path.slice(idx + 1);
}

/**
 * Apply a revision switch to an active-path message list.
 *
 * Returns `{ nextList, activeLeafId }`. This is the **single source of truth**
 * for both halves — callers must not recompute the leaf themselves.
 *
 * ``activeLeafId`` is the branch's deepest node as a *server* id, for
 * ``PUT .../active-leaf``. It is `null` whenever there is nothing legitimate
 * to persist:
 *   - the switch is a no-op (out of range / already active), or
 *   - the branch's deepest node has never been persisted (a local regenerate
 *     whose fork POST has not landed, so `dbId` is absent).
 * The second case must not fall back to the *previous* sibling's `dbId` —
 * that would point the server's active leaf at the branch the user just
 * switched away from.
 *
 * The merged row obeys the same rule: its ``dbId`` is the *incoming*
 * revision's id or nothing at all. The fork source moves to ``forkAnchorId``
 * so that dropping the old fallback does not break regenerate.
 */
export function applyRevisionSwitch(list, assistantMsg, nextIdx) {
  const revs = Array.isArray(assistantMsg.revisions) ? assistantMsg.revisions : [];
  if (nextIdx < 0 || nextIdx >= revs.length) {
    return { nextList: list, activeLeafId: null };
  }
  if (nextIdx === assistantMsg.activeRev) {
    return { nextList: list, activeLeafId: null };
  }
  const target = revs[nextIdx] || {};
  const idx = (list || []).findIndex((m) => m.id === assistantMsg.id);
  if (idx < 0) {
    return { nextList: list, activeLeafId: null };
  }
  const currentTail = list.slice(idx + 1);
  const updatedRevs = revs.map((r, i) =>
    i === assistantMsg.activeRev ? { ...r, tail: currentTail } : r,
  );
  const updatedAssistant = {
    ...list[idx],
    text: target.text || "",
    trace: target.trace || [],
    reasoning: target.reasoning || null,
    traceId: target.traceId,
    latencyMs: target.latencyMs,
    // No fallback: an unpersisted revision has no server row, and inheriting
    // the outgoing sibling's id would make every id-keyed action (rating,
    // active-leaf) hit the version the user just switched *away* from.
    dbId: persistedId(target.dbId),
    // Regenerate still needs somewhere to branch from — that lives here now.
    forkAnchorId: forkAnchorFor(updatedRevs, list[idx]),
    timestamp: target.timestamp,
    parentId: target.parentId ?? list[idx].parentId,
    activeRev: nextIdx,
    revisions: updatedRevs,
  };
  const tail = Array.isArray(target.tail) ? target.tail : [];
  const nextList = [...list.slice(0, idx), updatedAssistant, ...tail];
  const deepest = tail.length ? tail[tail.length - 1] : updatedAssistant;
  const activeLeafId = persistedId(deepest?.dbId) ?? null;
  return { nextList, activeLeafId };
}

/**
 * Attach `revisions` / `activeRev` on each active-path message from its
 * sibling set so the PR #50 version pager shows real N/M.
 * Each revision's ``tail`` is the real descendant chain under that sibling.
 */
export function attachRevisionsFromSiblings(activePath, allMessages) {
  return (activePath || []).map((msg) => {
    const sibs = siblingsOf(allMessages, msg);
    if (sibs.length <= 1) {
      return {
        ...msg,
        forkAnchorId: persistedId(msg.dbId),
        revisions: undefined,
        activeRev: undefined,
      };
    }
    const selfId = msgId(msg);
    const activeRev = Math.max(
      0,
      sibs.findIndex((s) => msgId(s) === selfId),
    );
    const revisions = sibs.map((s) => {
      const sid = msgId(s);
      return {
        text: s.text || s.content || "",
        trace: s.trace || (s.metadata && s.metadata.trace) || [],
        reasoning: s.reasoning || (s.metadata && s.metadata.reasoning) || null,
        traceId: s.traceId || s.trace_id,
        latencyMs: s.latencyMs || s.latency_ms,
        dbId: sid,
        timestamp: s.timestamp || s.createdAt || s.created_at,
        parentId: parentKeyOf(s),
        tail: descendantTailFromTree(allMessages, sid),
      };
    });
    return { ...msg, forkAnchorId: forkAnchorFor(revisions, msg), revisions, activeRev };
  });
}

/**
 * Hydrate a `?tree=1` conversation detail into the UI active-path list with
 * revision metadata for the version pager.
 */
export function hydrateMessagesFromTreeDetail(detail, mapServerMessage, conversationId) {
  const raw = (detail?.messages || []).map((m) => ({
    ...mapServerMessage(m),
    parentId: m.parent_id ?? null,
    conversationId,
  }));
  const path = activePathFromTree(raw, detail?.active_leaf_message_id ?? null);
  return attachRevisionsFromSiblings(path, raw);
}
