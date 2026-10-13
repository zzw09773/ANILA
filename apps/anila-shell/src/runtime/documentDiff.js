import { authMultipart } from "./api.js";

/**
 * 只留下對得到程式差異編號的說明。呼叫端或模型多寫的編號不會變成新卡片。
 * @param {Array<{ id?: string }> | null | undefined} changes
 * @param {Array<{ id?: string, text?: string }> | null | undefined} explanations
 */
export function visibleExplanations(changes, explanations) {
  const ids = new Set((changes || []).map((change) => change && change.id).filter(Boolean));
  const seen = new Set();
  const out = [];
  for (const item of explanations || []) {
    if (!item || typeof item.id !== "string" || !ids.has(item.id) || seen.has(item.id)) continue;
    const text = String(item.text || "").trim();
    if (!text) continue;
    seen.add(item.id);
    out.push({ id: item.id, text });
  }
  return out;
}

function formFor(beforeFile, afterFile) {
  const form = new FormData();
  form.append("before", beforeFile);
  form.append("after", afterFile);
  return form;
}

export function compareDocumentVersions(beforeFile, afterFile) {
  return authMultipart("/api/document-diffs", formFor(beforeFile, afterFile));
}

export function explainDocumentVersions(beforeFile, afterFile) {
  return authMultipart("/api/document-diffs/explain", formFor(beforeFile, afterFile));
}
