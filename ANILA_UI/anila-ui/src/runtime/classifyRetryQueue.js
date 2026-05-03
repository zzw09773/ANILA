// Sprint 8 X / Phase K — classified-latch retry queue.
//
// Background
// ----------
// applyMeta() fires POST /api/conversations/:id/classify as a side-effect
// when SSE meta.classified === true. The original implementation:
//   1. Gated on `typeof convId === "number"` so the call would silently
//      drop while the conversation was still on a client-side temp id
//      (the brief window between optimistic creation and the server
//      replying with a numeric id).
//   2. Swallowed any HTTP failure with `.catch(() => {})`.
//
// Combined effect: a conversation that locked classified during the
// first turn could lose the lock on reload because CSP never received
// the latch. This is a P0 — the README documents one-way latch as an
// invariant.
//
// This module hardens the persistence path:
//   * We still try to send immediately when convId is numeric.
//   * Failures + temp-id-at-meta-time both fall into a sessionStorage
//     queue keyed by client temp id.
//   * Once the optimistic id resolves to a numeric server id, the
//     queue is replayed.
//   * A page-focus listener also retries any straggling entries
//     (covers tab-switch / network-blip recovery).
//
// We deliberately use sessionStorage (not localStorage): a hard reload
// doesn't carry pending lock attempts across sessions because the
// SSE-derived classified flag would arrive again on the next
// streaming session anyway. sessionStorage scopes to the tab, exactly
// matching the lifecycle of an unsaved conversation.

const STORAGE_KEY = "anila.classifyRetry.v1";

/** @typedef {{ tempId: string|number, numericId: number|null, attempts: number, lastErrorAt?: string }} QueueEntry */

function _read() {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function _write(entries) {
  try {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(entries));
  } catch {
    // sessionStorage full or unavailable — degrade silently. The
    // latch still works in-memory; persistence loss is bounded.
  }
}

/**
 * Enqueue (or update) a pending classify attempt.
 *
 * @param {string|number} tempId  The convId at the moment meta arrived.
 *                                Either a client temp id ("u_…") or a
 *                                numeric server id depending on timing.
 * @param {{ numericId?: number|null }} [opts]
 */
export function enqueueClassifyRetry(tempId, opts = {}) {
  const { numericId = null } = opts;
  const entries = _read();
  const idx = entries.findIndex((e) => String(e.tempId) === String(tempId));
  const next = {
    tempId: typeof tempId === "number" ? tempId : String(tempId),
    numericId: typeof numericId === "number" ? numericId : null,
    attempts: idx >= 0 ? entries[idx].attempts + 1 : 1,
    lastErrorAt: new Date().toISOString(),
  };
  if (idx >= 0) entries[idx] = next;
  else entries.push(next);
  _write(entries);
}

/**
 * Tell the queue that a temp id has resolved to a numeric server id.
 * Replays any pending entry for that tempId.
 *
 * @param {string|number} tempId
 * @param {number} numericId
 * @param {(numericId: number) => Promise<unknown>} sender
 */
export async function resolveTempId(tempId, numericId, sender) {
  const entries = _read();
  const match = entries.find((e) => String(e.tempId) === String(tempId));
  if (!match) return;
  // Replace tempId → numericId so subsequent retries are addressable.
  match.tempId = numericId;
  match.numericId = numericId;
  _write(entries);
  await flushOne(match, sender);
}

/**
 * Send one entry. On success, drop it; on failure, leave it in place
 * with an incremented attempt counter so the next focus / next
 * resolveTempId picks it up again.
 *
 * @param {QueueEntry} entry
 * @param {(numericId: number) => Promise<unknown>} sender
 */
async function flushOne(entry, sender) {
  if (!entry || typeof entry.numericId !== "number") return;
  try {
    await sender(entry.numericId);
    const after = _read().filter(
      (e) => String(e.tempId) !== String(entry.tempId)
    );
    _write(after);
  } catch (err) {
    enqueueClassifyRetry(entry.tempId, { numericId: entry.numericId });
    // eslint-disable-next-line no-console
    console.error("[classified-latch] retry failed", err);
  }
}

/**
 * Walk the queue and retry every entry that already has a numericId.
 * Skips entries that still hold a temp id (those wait for resolveTempId).
 *
 * @param {(numericId: number) => Promise<unknown>} sender
 */
export async function flushAll(sender) {
  const entries = _read();
  const ready = entries.filter((e) => typeof e.numericId === "number");
  for (const entry of ready) {
    // eslint-disable-next-line no-await-in-loop
    await flushOne(entry, sender);
  }
}

/**
 * Wire a window-focus listener that calls flushAll on every focus
 * event. Returns the disposer.
 *
 * @param {(numericId: number) => Promise<unknown>} sender
 * @returns {() => void}
 */
export function installFocusFlush(sender) {
  if (typeof window === "undefined") return () => {};
  const handler = () => {
    flushAll(sender).catch(() => {
      // flushAll swallows individual failures already; this catch is
      // belt-and-braces for any synchronous throw.
    });
  };
  window.addEventListener("focus", handler);
  return () => window.removeEventListener("focus", handler);
}

// For tests + inspection.
export const __internal = { _read, _write, STORAGE_KEY };
