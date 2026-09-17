/** Persist contract for metadata.reasoning — produced by CSP, shown by Shell. */

export const REASONING_PERSIST_LIVE_NOTICE =
  "思考過長，僅前段已存入紀錄；重新整理後無法查看全文。";

export const REASONING_PERSIST_RELOAD_NOTICE = "僅保存思考前段";

export const REASONING_PERSIST_OMITTED_NOTICE = "思考過長，未存入對話紀錄。";

export function readReasoningPersist(input) {
  const persist = input?.reasoningPersist || input?.reasoning_persist || null;
  if (!persist || typeof persist !== "object") return null;
  const status = persist.status;
  if (status !== "full" && status !== "truncated" && status !== "omitted") {
    return null;
  }
  return persist;
}

/** True when this page still holds more text than the stored prefix. */
export function isLiveReasoningOverflow(persist, reasoning) {
  if (!persist || persist.status === "full") return false;
  const text = typeof reasoning === "string" ? reasoning : "";
  if (persist.status === "omitted") return text.length > 0;
  const kept = typeof persist.kept_chars === "number" ? persist.kept_chars : 0;
  return text.length > kept;
}

export function reasoningPersistNotice(persist, reasoning) {
  const p = readReasoningPersist({ reasoningPersist: persist });
  if (!p || p.status === "full") return null;
  if (p.status === "omitted" && !String(reasoning || "").trim()) {
    return REASONING_PERSIST_OMITTED_NOTICE;
  }
  if (isLiveReasoningOverflow(p, reasoning)) {
    return REASONING_PERSIST_LIVE_NOTICE;
  }
  if (p.status === "truncated") return REASONING_PERSIST_RELOAD_NOTICE;
  if (p.status === "omitted") return REASONING_PERSIST_OMITTED_NOTICE;
  return null;
}

export function canExpandPersistedReasoning(persist, reasoning, hasTrace) {
  if (hasTrace) return true;
  return typeof reasoning === "string" && reasoning.length > 0;
}

export function shouldKeepLiveReasoning(persist, localReasoning, storedReasoning) {
  const p = readReasoningPersist({ reasoningPersist: persist });
  if (!p || p.status === "full") return false;
  const local = typeof localReasoning === "string" ? localReasoning : "";
  const stored = typeof storedReasoning === "string" ? storedReasoning : "";
  return local.length > stored.length;
}

export function persistFieldsFromSaved(saved) {
  const persist = readReasoningPersist(saved?.metadata || {});
  if (!persist) return null;
  return { reasoningPersist: persist };
}
