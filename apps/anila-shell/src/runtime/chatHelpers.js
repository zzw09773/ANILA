// Pure helpers for the chat runtime (split out of app.jsx, no behaviour change).
// app.jsx re-exports the public ones so existing imports keep working.

import { AGENT_REPLY_OBSERVATION_KEY, agentReplyNotice } from "./agentReplySignal.js";
import { visibleStarterPrompts } from "../starterPrompts.js";

export const ROUTER_AGENT = Object.freeze({
  id: "anila-router",
  name: "ANILA",
  short: "auto",
  description: "ANILA 會幫你找合適的助手，也可能直接回答你。",
  requiresEncryption: false,
});

// 入門卡只列出程式已從合成示範文件查到原文的問句。查不到的不會出現。
// `_agents` 留著是因為空狀態本來就會把助手清單傳進來；清單不再決定卡片。
export function buildStarterPrompts(_agents) {
  return visibleStarterPrompts().map((prompt, index) => ({
    id: prompt.id,
    title: prompt.title,
    sub: prompt.sub,
    q: prompt.q,
    primary: index === 0,
  }));
}

export function makeId(prefix) {
  return `${prefix}-${Math.random().toString(16).slice(2, 10)}-${Date.now().toString(36)}`;
}

export function streamBusyError() {
  const err = new Error("對話正在產生回覆，請等待完成");
  err.code = "stream-busy";
  return err;
}

export function bulkAccountNote(errors) {
  for (const error of errors || []) {
    if (!error) continue;
    const message = typeof error.message === "string" ? error.message : "";
    if (error.status === 409 && message.includes("登入帳號已變更")) return message;
    if (error.code === "expected-user-missing" && message) return message;
  }
  return "";
}

export function bulkDeleteStatus({ succeeded, apiFailed, waiting, accountNote = "" }) {
  const ok = succeeded.length;
  const bad = apiFailed.length;
  const wait = waiting.length;
  let text;
  if (bad === 0 && wait === 0) text = `已刪除 ${ok} 則對話。`;
  else if (bad === 0) {
    text = ok === 0
      ? "選取的對話正在產生回覆，請等待完成後再刪除。"
      : `已刪除 ${ok} 則，${wait} 則正在產生回覆，請等待完成。尚未完成的對話仍保留。`;
  } else if (wait === 0) {
    text = `已刪除 ${ok} 則，${bad} 則失敗。失敗的對話仍保留，可以再試一次。`;
  } else {
    text = `已刪除 ${ok} 則，${bad} 則失敗，${wait} 則正在產生回覆，請等待完成。失敗的對話仍保留，可以再試一次。`;
  }
  return accountNote ? `${text}${accountNote}` : text;
}

export function staleAccountError() {
  const err = new Error("登入帳號已變更，請重新整理後再操作");
  err.status = 409;
  return err;
}

export const BULK_RELOGIN_MESSAGE = "請重新登入或重新整理後再操作。";

export function makeConversationTitle(text) {
  const t = (text || "").trim();
  if (!t) return "新對話";
  return t.length > 28 ? `${t.slice(0, 28)}…` : t;
}

/** reference_id values from composer chips (or AttachmentOut-shaped rows). */
export function attachmentBindIds(attachments) {
  const ids = [];
  const seen = new Set();
  for (const a of attachments || []) {
    if (!a || typeof a !== "object") continue;
    const rid = a.referenceId || a.reference_id;
    const fallback = typeof a.id === "string" ? a.id : null;
    const value = typeof rid === "string" && rid.trim() ? rid.trim() : fallback;
    if (typeof value !== "string" || !value.trim()) continue;
    const key = value.trim();
    if (seen.has(key)) continue;
    seen.add(key);
    ids.push(key);
  }
  return ids;
}

export function nowIso() {
  return new Date().toISOString();
}

// Sprint 7 X follow-up：API key UI 已下線（cookie 流程後 SPA 不持有 key），
// 因此原本的 maskApiKey helper 也一併移除，避免 UI 仍假裝可以管理 key。

// normalize backend /v1/agents payload → UI agent model
// includes the ROUTER pseudo-agent in front
export function normalizeAgents(data) {
  return [
    ROUTER_AGENT,
    ...(data || []).map((item) => ({
      id: item.id,
      name: item.name || item.id,
      short: (item.short || item.id || "").slice(0, 12),
      description: item.description_for_router || item.description || "",
      endpointUrl: item.endpoint_url,
      capabilities: item.capabilities || {},
      requiresEncryption: Boolean(item.requires_encryption),
    })),
  ];
}

/**
 * 院內規章檢索的三個欄位,從 `anila_meta` 抄到 UI 的訊息列上。
 *
 * meta 有**兩個**入口,而且兩個都必須抄:重新載入走 `mapServerMessage`、
 * SSE 現場走 `applyMeta`。只接一邊不會有任何錯誤訊息,只會讓同一則答案在
 * 串完的當下說「依據是人事管理規則」、重新整理之後說不出話來(或反過來)。
 * 抽成一個函式而不是在兩處各寫一次,是為了讓「兩邊講的是同一件事」變成
 * 結構上的事實,而不是靠人記得同步兩份欄位清單。
 *
 * ⚠ **狀態只從 `kb_state` 抄,不從資料反推。** 生產端的事實
 * (`services/csp/app/services/institutional_kb.py:226-233`):`partial_error`
 * **一定帶著命中**,`search_error` 有失敗的庫而沒有命中。所以「有 kb_hits
 * 就當命中」會把 `partial_error` 講成乾淨的命中,使用者就永遠看不到
 * 「這不是全部的依據」——那正是他最需要知道的一句話。
 *
 * `citations` 不在這裡:兩個縫本來就各自映射了那個欄位,CSP 命中時是把
 * 規章**前綴**進既有的 citations(不覆蓋下游來源),沿用原本的管線即可。
 *
 * @param {object | null | undefined} meta - 一份 `anila_meta`
 */
export function kbMetaFields(meta) {
  const m = meta && typeof meta === "object" ? meta : {};
  return {
    // 缺席就是缺席:這個功能上線前存下來的訊息沒有這個欄位,而
    // `undefined` 仍在畫面上保持安靜;明帶的 `"not_searched"` 則由 chat.jsx
    // 揭露「本次未檢索院內規章」,兩者不可混為一談。
    kbState: typeof m.kb_state === "string" ? m.kb_state : undefined,
    kbHits: Array.isArray(m.kb_hits) ? m.kb_hits : [],
    kbFailedCollections: Array.isArray(m.kb_failed_collections)
      ? m.kb_failed_collections
      : [],
    keywordFallback: m.keyword_fallback === true,
  };
}

/**
 * The registered-agent observation has the same two ingress seams as KB meta:
 * server-message reload and live SSE.  Unknown or absent observations remain
 * silent so old messages do not acquire a new claim.
 */
export function injectionMetaFields(meta) {
  const m = meta && typeof meta === "object" ? meta : {};
  return {
    promptInjectionSuspected: m.prompt_injection_suspected === true,
  };
}

export const liveThinkingPumpMaps = new Set();

export function liveThinkingPumpCount() {
  let count = 0;
  for (const map of liveThinkingPumpMaps) count += map.size;
  return count;
}

export function agentReplyMetaFields(meta) {
  const m = meta && typeof meta === "object" ? meta : {};
  return {
    agentReplyObservation: m[AGENT_REPLY_OBSERVATION_KEY],
    agentReplyNotice: agentReplyNotice(m),
  };
}
