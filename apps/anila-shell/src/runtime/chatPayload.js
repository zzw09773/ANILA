// OpenAI-shaped payload builders for a chat turn (split out of app.jsx).

import { COMPACT_SUMMARY_PREFIX } from "./compact.js";

// ---- Helpers ---------------------------------------------------------------
// Build an OpenAI chat message `content` for a user turn. When the message
// has no image attachments we keep the string form (compatible with every
// model). When images are present we switch to the array form with
// `image_url` parts so vision-capable models (Gemma4, gpt-4o, ...) can see
// the image inline. Non-image attachments are referenced by filename in a
// trailing text note.
// Fold prior conversation turns (excluding the live streaming assistant)
// into the OpenAI message history so the model remembers what was said —
// and, critically, so images from earlier turns stay visible.
/**
 * 從一個「要送出去的 payload」裡取出**這一輪使用者新講的那段話**。
 *
 * 敏感資訊閘門看的就是這一段:不是整串歷史(那會讓任何一個曾經出現過個資的
 * 對話再也不能重試),而是使用者這次提交的內容。
 *
 * 刻意只認 payload 本身,不讓呼叫端多傳任何東西 —— 呼叫端一旦能選擇「傳不傳」,
 * 這個閘門就退化回一份要大家記得去呼叫的清單,而那份清單已經漏掉兩次了。
 */
export function outgoingUserText(payload) {
  const msgs = Array.isArray(payload?.messages) ? payload.messages : [];
  for (let i = msgs.length - 1; i >= 0; i -= 1) {
    const m = msgs[i];
    if (!m || m.role !== "user") continue;
    const c = m.content;
    if (typeof c === "string") return c;
    if (Array.isArray(c)) {
      return c
        .filter((p) => p && p.type === "text")
        .map((p) => p.text || "")
        .join("\n");
    }
    return "";
  }
  return "";
}

/**
 * 組出送給 Router 的 OpenAI messages。
 *
 * 刻意不走這函式（不帶對話歷史）的呼叫端：
 * 1. generateConversationTitle — 標題產生器，自帶 system＋單則 Q&A
 * 2. buildDeclarativeActionMessages / runActionInvokeFillback — 宣告式 custom action，只有渲染後的 prompt
 * 3. sendCompare — 比較／direct 並排，每欄只送當則 user
 *
 * CSP `POST /api/conversations/{id}/messages`（append_message）的 role pattern
 * 含 tool，且會原樣持久化。這裡把 persisted `role:"tool"` 轉成帶
 * `tool_result` block 的 user 訊息；Router `_split_turns` 會把它併進前一回合，
 * 不新開回合、不影響 `kept_from_index`。
 */
export function buildMessageHistory(priorMsgs, currentText, currentAttachments, options = null) {
  const out = [];
  const sources = [];
  for (const m of priorMsgs || []) {
    if (!m || m.streaming) continue;
    if (m.role === "user") {
      out.push({ role: "user", content: buildUserContent(m.text || "", m.attachments || []) });
      sources.push(m);
    } else if (m.role === "assistant" && m.text) {
      out.push({ role: "assistant", content: m.text });
      sources.push(m);
    } else if (m.role === "tool") {
      const toolUseId = m.toolCallId || m.tool_call_id || (
        typeof m.dbId === "number" ? String(m.dbId) : String(m.id ?? "")
      );
      out.push({
        role: "user",
        content: [{
          type: "tool_result",
          tool_use_id: toolUseId,
          content: m.text || "",
        }],
      });
      sources.push(m);
    }
  }
  if (currentText !== null && currentText !== undefined) {
    out.push({ role: "user", content: buildUserContent(currentText, currentAttachments) });
    sources.push(options?.currentUserMsg || null);
  }
  const compact = options?.compact;
  const boundaryId = compact?.boundaryMessageId;
  if (
    compact?.summary
    && boundaryId != null
    && sources.some((m) => m && m.dbId === boundaryId)
  ) {
    const idx = sources.findIndex((m) => m && m.dbId === boundaryId);
    const kept = out.slice(idx);
    const keptSources = sources.slice(idx);
    const messages = [
      { role: "system", content: `${COMPACT_SUMMARY_PREFIX}${compact.summary}` },
      ...kept,
    ];
    options?.onBuilt?.({ messages, sources: keptSources });
    return messages;
  }
  options?.onBuilt?.({ messages: out, sources });
  return out;
}

export function buildUserContent(text, attachments) {
  const list = Array.isArray(attachments) ? attachments : [];
  const images = list.filter((a) => a.dataUrl && (a.kind === "image" || (a.contentType || "").startsWith("image/")));
  const otherFiles = list.filter((a) => !images.includes(a) && a.name);
  if (images.length === 0) {
    if (otherFiles.length === 0) return text;
    const tail = otherFiles.map((a) => `- ${a.name}`).join("\n");
    return `${text}\n\n[附件]\n${tail}`;
  }
  const parts = [{ type: "text", text: text || "" }];
  for (const img of images) {
    parts.push({ type: "image_url", image_url: { url: img.dataUrl } });
  }
  if (otherFiles.length > 0) {
    parts[0].text = `${parts[0].text}\n\n[附件]\n${otherFiles.map((a) => `- ${a.name}`).join("\n")}`;
  }
  return parts;
}
