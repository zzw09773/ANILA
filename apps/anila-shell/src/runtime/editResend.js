// Edit-resend gate for rewriting a user message (OW-1 sibling branch).
// Owner 2026-08-01: identical text is a legitimate re-send; only empty aborts.
// Call sites (chat.jsx saveEdit, app.jsx handleEditUser) must go through this
// helper so tests can pin the invariant without mounting ChatRuntime.

/**
 * Normalize an edit draft and decide whether to proceed with re-send.
 *
 * @param {unknown} nextText - draft from the editor (or handler argument)
 * @returns {{ ok: false } | { ok: true, text: string }}
 */
export function resolveEditResend(nextText) {
  const text = String(nextText ?? "").trim();
  if (!text) return { ok: false };
  return { ok: true, text };
}
