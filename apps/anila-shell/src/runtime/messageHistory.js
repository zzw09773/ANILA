// OpenAI-style history builder shared by send / edit / regenerate (W2-3 A3).

/**
 * Fold prior conversation turns (excluding the live streaming assistant)
 * into the OpenAI message history so the model remembers what was said —
 * and, critically, so images from earlier turns stay visible.
 *
 * `buildUserContent` is injected to avoid circular imports with app.jsx.
 */
export function buildMessageHistory(priorMsgs, currentText, currentAttachments, buildUserContent) {
  const out = [];
  for (const m of priorMsgs || []) {
    if (!m || m.streaming) continue;
    if (m.role === "user") {
      out.push({
        role: "user",
        content: buildUserContent(m.text || "", m.attachments || []),
      });
    } else if (m.role === "assistant" && m.text) {
      out.push({ role: "assistant", content: m.text });
    }
  }
  out.push({
    role: "user",
    content: buildUserContent(currentText, currentAttachments),
  });
  return out;
}

/**
 * Edit re-run payload: full ACTIVE PATH history up to (but not including)
 * the edited user turn, plus the new user text. Must not be a single sentence.
 */
export function buildEditRerunMessages(
  activePathMsgs,
  editedUserIdx,
  nextText,
  attachments,
  buildUserContent,
) {
  const prior = (activePathMsgs || []).slice(0, Math.max(0, editedUserIdx));
  return buildMessageHistory(prior, nextText, attachments || [], buildUserContent);
}
