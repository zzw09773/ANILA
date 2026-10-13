// Map CSP conversation / message rows into the shape ChatRuntime keeps in state.
// Pure functions: everything they need comes in as arguments.

import { appliedSkillFromMeta } from "./skills.js";
import { appendClassifiedTag, computeConversationClassified } from "./classified.js";
import { messageDocument } from "./messageMeta.js";
import { relativeLabel } from "./time.js";
import { compactFieldsFromServer } from "./compact.js";
import { mapServerAttachments } from "./messageAttachments.js";
import { readStreamState, streamStateNotice } from "./reservedTurn.js";
import { normalizeThinkingTier } from "./thinkingTier.js";
import { normalizeInterrupt } from "../agentic.jsx";
import { readThinkingStages } from "./thinkingStages.js";
import {
  nowIso,
  kbMetaFields,
  injectionMetaFields,
  sourceCheckFields,
  agentReplyMetaFields,
} from "./chatHelpers.js";

// Map a backend ConversationOut row → the sidebar shape the UI already uses.
//
// Classification follows the latch invariant in runtime/classified.js:
// ANY truthy signal (server-persisted classified + agent.requires_encryption)
// wins. Without this OR, a hard refresh that races the agents fetch ahead
// of the conversations fetch would silently drop encryption mode for
// conversations whose classified=true wasn't (yet) persisted on the row.
export function mapServerConversation(serverRow, agentNameLookup, agentRequiresEncryptionLookup) {
  const agentName =
    serverRow.agent_id != null ? agentNameLookup(serverRow.agent_id) : null;
  const agentRequiresEncryption =
    serverRow.agent_id != null && agentRequiresEncryptionLookup
      ? Boolean(agentRequiresEncryptionLookup(serverRow.agent_id))
      : false;
  const classified = computeConversationClassified(
    { classified: serverRow.classified },
    { agentRequiresEncryption },
  );
  return {
    id: serverRow.id,
    title: serverRow.title,
    ts: relativeLabel(),
    updatedLabel: relativeLabel(),
    agent: agentName || null,
    agentId: serverRow.agent_id || null,
    agentName: agentName || null,
    folder: typeof serverRow.folder === "string" && serverRow.folder
      ? serverRow.folder
      : "all",
    tags: (() => {
      const raw = Array.isArray(serverRow.tags) ? serverRow.tags.filter(Boolean) : [];
      const userTags = raw.filter((t) => t !== "classified");
      return classified ? appendClassifiedTag(userTags) : userTags;
    })(),
    starred: Boolean(serverRow.starred),
    classified,
    // P3: distinguishes inheritance-driven latch from agent-required
    // or admin-set classification. Drives the warning banner copy
    // and the (lighter-weight) lock icon variant on the sidebar.
    classificationInherited: Boolean(serverRow.classification_inherited),
    // Slice 3c: four-level classification label (added by the multi-level
    // classification backend). Absent on boolean-only payloads → the level
    // badge simply renders nothing; the boolean latch above is unaffected.
    classificationLevel: serverRow.classification_level,
    // OW-1: server-truth active leaf pointer for the message tree.
    // ⚠ 這個欄位在 shell 裡**沒有任何讀取者**(2026-08-05 全樹確認):路徑一律
    // 由伺服器回傳的 messages 決定,leaf 只是鏡像。保留是因為它零成本且是
    // 除錯時唯一看得到的伺服器指標;動它不會改變任何畫面,所以也不值得測。
    activeLeafMessageId: serverRow.active_leaf_message_id ?? null,
    updatedAt: serverRow.updated_at || serverRow.created_at || nowIso(),
    routerModelId: serverRow.router_model_id ?? null,
    routerModelName: serverRow.router_model_name ?? null,
    routerModelDisplayName: serverRow.router_model_display_name || null,
    routerModelUnavailableReason: serverRow.router_model_unavailable_reason || null,
    routerSelectionVersion: serverRow.router_selection_version ?? 0,
    thinkingTier: normalizeThinkingTier(serverRow.thinking_tier),
    ...compactFieldsFromServer({
      compact_summary: serverRow.compact_summary ?? null,
      compact_boundary_message_id: serverRow.compact_boundary_message_id ?? null,
      compact_updated_at: serverRow.compact_updated_at ?? null,
    }),
  };
}

export function mapServerMessage(msg) {
  const meta = msg.metadata || {};
  return {
    id: `srv-${msg.id}`,
    dbId: msg.id,
    role: msg.role,
    text: msg.content || "",
    toolCallId: msg.tool_call_id || meta.tool_call_id || null,
    // OW-1 tree nav fields (MessageOut).
    parentId: msg.parent_id ?? null,
    siblingIndex: typeof msg.sibling_index === "number" ? msg.sibling_index : 0,
    siblingCount: typeof msg.sibling_count === "number" ? msg.sibling_count : 1,
    siblingIds: Array.isArray(msg.sibling_ids) ? msg.sibling_ids : [msg.id],
    trace: meta.trace || [],
    citations: meta.citations || [],
    followUps: meta.follow_ups || [],
    handoffChain: meta.handoff_chain || [],
    // 重新載入這一縫。同一份定義也要接在 applyMeta(SSE 現場那一縫)上。
    ...kbMetaFields(meta),
    ...agentReplyMetaFields(meta),
    ...injectionMetaFields(meta),
    ...sourceCheckFields(meta),
    confidence: meta.confidence,
    classified: meta.classified,
    traceId: msg.trace_id || meta.trace_id,
    latencyMs: msg.latency_ms,
    routedAgentId: meta.routed_agent_id || null,
    rating: msg.rating || null,
    ratingScore: typeof msg.rating_score === "number" ? msg.rating_score : null,
    reasoning: meta.reasoning || null,
    reasoningPersist: meta.reasoning_persist || null,
    thinkingSummaries: Array.isArray(meta.thinking_summaries) ? meta.thinking_summaries : [],
    thinkingStages: readThinkingStages(meta),
    thinkingStatus: meta.thinking_status || null,
    thinkingElapsedMs: typeof meta.thinking_elapsed_ms === "number" ? meta.thinking_elapsed_ms : null,
    document: messageDocument(meta),
    thinkingLocked: meta.thinking_locked === true,
    appliedSkill: appliedSkillFromMeta(meta),
    usage: meta.usage || null,
    thinkingApplied: meta.thinking_applied || null,
    finishReason:
      meta.finish_reason === "length" || meta.finish_reason === "stop"
        ? meta.finish_reason
        : null,
    interrupt: normalizeInterrupt(meta.interrupt),
    settledInterrupts: Array.isArray(meta.settled_interrupts)
      ? meta.settled_interrupts.map((item) => normalizeInterrupt(item)).filter(Boolean)
      : [],
    askContentSpans: Array.isArray(meta.ask_content_spans) ? meta.ask_content_spans : null,
    prefaceText: typeof meta.resume_preface === "string" ? meta.resume_preface : null,
    resumeText: resumeTextFromPreface(meta.resume_preface, msg.content),
    sessionId: meta.interrupt?.session_id || meta.session_id || null,
    // OW-3: action:NAME attribution (second channel alongside metadata.action).
    agentName: msg.agent_name || null,
    // OW-3 provenance (metadata.action) — quiet action-name attribution.
    metadata: meta,
    // 先預留再串流:一則從伺服器載回來的助理訊息可能是半截的(使用者按
    // 停止、串流出錯、寫它的分頁消失)。狀態如實帶進 UI,半截的答案一定
    // 要標示出來,不能長得跟完整答案一樣。
    streamState: readStreamState(meta),
    incompleteNotice: streamStateNotice(
      readStreamState(meta),
      Boolean(msg.content)
        || Boolean(meta.interrupt)
        || (Array.isArray(meta.settled_interrupts) && meta.settled_interrupts.length > 0),
    ),
    streaming: false,
    attachments: mapServerAttachments(msg.attachments),
    attachmentAdmitted:
      meta.attachment_admitted && typeof meta.attachment_admitted === "object"
        ? meta.attachment_admitted
        : null,
    conversationId: null, // patched by caller
    createdAt: msg.created_at,
  };
}

export function resumeTextFromPreface(preface, content) {
  if (typeof preface !== "string") return null;
  const text = content || "";
  if (!text.startsWith(preface)) return null;
  return text.slice(preface.length).replace(/^\n/, "");
}

export function rememberMovedConversation(prev, id, folderId, row) {
  let found = false;
  const next = prev.map((c) => {
    if (c.id !== id) return c;
    found = true;
    return { ...c, folder: folderId };
  });
  if (found) return next;
  next.push({
    id,
    title: row?.title || "對話",
    folder: folderId,
    tags: Array.isArray(row?.tags) ? row.tags : [],
    starred: Boolean(row?.starred),
    classified: Boolean(row?.classified),
    updatedAt: row?.updatedAt || row?.updated_at || new Date().toISOString(),
    createdAt: row?.createdAt || row?.created_at,
    agentId: row?.agentId ?? row?.agent_id ?? null,
    agent: row?.agent ?? null,
  });
  return next;
}
