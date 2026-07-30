// Compare-mode 「採用此回答」— promote on-screen text into a real conversation.
//
// Decision: promote (not re-send). The user is keeping a specific answer they
// already saw; re-calling the model would wait again and may diverge. Compare
// columns stay ephemeral — only the adopted column is persisted.
//
// Classification is server-truth: the adopt endpoint latches from agent policy.
// This helper never invents `classified: true` on the client.

import { buildPersistMeta } from "./messageMeta.js";

/**
 * Pick the latest completed assistant turn and its paired user question
 * from a compare column's local message list (compare appends rounds).
 * @returns {{ ok: true, user: object, assistant: object } | { ok: false, error: string }}
 */
export function selectAdoptMessages(msgs) {
  const list = Array.isArray(msgs) ? msgs : [];
  if (!list.length) {
    return { ok: false, error: "此欄尚無內容可採用" };
  }
  let assistantIdx = -1;
  for (let i = list.length - 1; i >= 0; i -= 1) {
    const m = list[i];
    if (m?.role !== "assistant") continue;
    if (m.streaming) {
      return { ok: false, error: "回答尚未完成，無法採用" };
    }
    if (m.error) {
      return { ok: false, error: "此欄回答失敗，無法採用" };
    }
    assistantIdx = i;
    break;
  }
  if (assistantIdx < 0) {
    return { ok: false, error: "採用內容缺少助理訊息" };
  }
  let user = null;
  for (let i = assistantIdx - 1; i >= 0; i -= 1) {
    if (list[i]?.role === "user") {
      user = list[i];
      break;
    }
  }
  if (!user || !(user.text || "").trim()) {
    return { ok: false, error: "採用內容缺少使用者訊息" };
  }
  return { ok: true, user, assistant: list[assistantIdx] };
}

/**
 * Call the adopt API with the selected column's on-screen text.
 * On failure throws (caller must not mutate conversation state).
 *
 * @param {object} opts
 * @param {(path: string, init?: object) => Promise<object>} opts.authRequest
 * @param {typeof import("./conversations.js").adoptConversation} opts.adoptConversation
 * @param {string} opts.agentId - compare column agent id (usually Agent.name)
 * @param {string} [opts.agentDisplayName]
 * @param {object[]} opts.msgs - compareMsgs[col.id]
 * @param {(text: string) => string} [opts.makeTitle]
 * @returns {Promise<object>} ConversationDetail from the server
 */
export async function promoteAdoptedAnswer({
  authRequest,
  adoptConversation,
  agentId,
  agentDisplayName,
  msgs,
  makeTitle,
}) {
  const picked = selectAdoptMessages(msgs);
  if (!picked.ok) {
    const err = new Error(picked.error);
    err.code = "adopt_precondition";
    throw err;
  }
  const { user, assistant } = picked;
  const titleSource = (user.text || "").trim();
  const title =
    typeof makeTitle === "function"
      ? makeTitle(titleSource)
      : titleSource.slice(0, 40) || "採用比較結果";

  const persistMeta = buildPersistMeta(
    {
      classified: assistant.classified === true ? true : undefined,
      trace_id: assistant.traceId,
      latency_ms: assistant.latencyMs,
      citations: assistant.citations,
      confidence: assistant.confidence,
      handoff_chain: assistant.handoffChain,
      follow_ups: assistant.followUps,
      reasoning: assistant.reasoning,
    },
    {
      trace: assistant.trace,
      reasoning: assistant.reasoning,
      classified: assistant.classified,
      citations: assistant.citations,
      handoffChain: assistant.handoffChain,
      followUps: assistant.followUps,
    },
  );

  return adoptConversation(authRequest, {
    title,
    agentName: typeof agentId === "string" ? agentId : undefined,
    agentId: typeof agentId === "number" ? agentId : undefined,
    userContent: user.text,
    assistantContent: assistant.text ?? "",
    assistantMetadata: persistMeta,
    assistantTraceId: assistant.traceId,
    assistantLatencyMs: assistant.latencyMs,
    assistantAgentName: agentDisplayName || String(agentId || ""),
  });
}
