// 回答底部的涵蓋列。數字只從已經在訊息上的檢索資料算出來，
// 不讀模型正文裡自己講的「查了幾份」。
//
// 查了／送進模型：`kb_hits` 是檢索留下、並且整批注入模型的段落。
// 現有 metadata 沒有另一份「檢索到但沒送進去」的清單，所以這兩個
// 數字都是這些段落裡的文件數（同一份文件多段只算一份）。
// 引用：正文裡的 [N] 對得上 `citations[N-1]` 的，才算引用到那份文件。
// 對不上的編號不當成引用。

import { visibleAskParts } from "./askTranscript.js";

const KNOWN_KB_STATES = new Set([
  "searched_hit",
  "searched_miss",
  "search_error",
  "partial_error",
  "not_searched",
]);

// 與 services/csp/app/services/document_excerpt.py 的同一句。
const EXCERPT_NOTICE = "以下是文件摘錄，不是全文。";
const KB_CITATION_ID = /^kb:\d+:(\d+):\d+$/;

function stripThinkTags(text) {
  return text
    .replace(/<think(?:ing)?>[\s\S]*?<\/think(?:ing)?>/gi, "")
    .replace(/<think(?:ing)?>[\s\S]*$/gi, "");
}

function visibleAnswerText(msg) {
  const parts = visibleAskParts(msg);
  const source = parts.preface != null
    ? [parts.preface, parts.continuation].filter((piece) => piece).join("\n")
    : (parts.body || "");
  return stripThinkTags(source);
}

function hitDocumentKey(hit, index) {
  if (!hit || typeof hit !== "object") return `anon:${index}`;
  const rawId = hit.document_id;
  if (typeof rawId === "number" && Number.isFinite(rawId)) return `doc:${rawId}`;
  if (typeof rawId === "string" && rawId.trim()) return `doc:${rawId.trim()}`;
  const filename = typeof hit.filename === "string" ? hit.filename.trim() : "";
  if (filename) return `file:${filename}`;
  return `anon:${index}`;
}

function citationDocumentKey(citation) {
  if (!citation || typeof citation !== "object") return null;
  const id = typeof citation.id === "string" ? citation.id.trim() : "";
  const kb = KB_CITATION_ID.exec(id);
  if (kb) return `doc:${kb[1]}`;
  const rawId = citation.document_id;
  if (typeof rawId === "number" && Number.isFinite(rawId)) return `doc:${rawId}`;
  if (typeof rawId === "string" && rawId.trim()) return `doc:${rawId.trim()}`;
  const title = typeof citation.title === "string" ? citation.title.trim() : "";
  if (title) return `file:${title}`;
  if (id) return `cite:${id}`;
  return null;
}

function excerptedFilenames(hits) {
  const names = [];
  const seen = new Set();
  for (const hit of hits) {
    const content = typeof hit?.content === "string" ? hit.content : "";
    if (!content.includes(EXCERPT_NOTICE)) continue;
    const filename = typeof hit?.filename === "string" ? hit.filename.trim() : "";
    if (!filename || seen.has(filename)) continue;
    seen.add(filename);
    names.push(filename);
  }
  return names;
}

// 平台知識庫沒有命中、也沒有對得上的引用時，不能說成「沒有來源」：
// 助手（尤其外部 agent）可能用自己的資料庫或工具，只是沒有列出引用。
export const NO_TRACEABLE_CITATION = "本次未列出可追溯的文件引用（助手可能使用自己的資料庫或工具）。";

function toolNames(msg) {
  const calls = Array.isArray(msg?.toolCalls)
    ? msg.toolCalls
    : (Array.isArray(msg?.tool_calls) ? msg.tool_calls : []);
  const names = [];
  for (const call of calls) {
    const raw = call?.display_name || call?.name || call?.tool || call?.function?.name;
    const name = typeof raw === "string" ? raw.trim() : "";
    if (name && !names.includes(name)) names.push(name);
    if (names.length >= 5) break;
  }
  return names;
}

export function formatCoverageLine({
  searched,
  sent,
  cited,
  excerpted = [],
  attachmentsOmitted = false,
  tools = [],
}) {
  const toolPart = tools.length > 0 ? `這次用了工具：${tools.join("、")}。` : "";
  if (searched === 0 && cited === 0) {
    const parts = [NO_TRACEABLE_CITATION];
    if (toolPart) parts.push(toolPart);
    if (attachmentsOmitted) parts.push("附件未納入這次回答。");
    return parts.join("");
  }
  const parts = [
    `查了 ${searched} 份、引用 ${cited} 份；送進模型 ${sent} 份。`,
    "未找到不等於不存在。",
  ];
  if (toolPart) parts.push(toolPart);
  if (attachmentsOmitted) parts.push("附件未納入這次回答。");
  if (excerpted.length > 0) {
    parts.push(`摘錄、不是全文：${excerpted.join("、")}。`);
  }
  return parts.join("");
}

/**
 * @param {object | null | undefined} msg
 * @param {{ attachmentsOmitted?: boolean }} [options]
 * @returns {{ searched: number, sent: number, cited: number, excerpted: string[], line: string } | null}
 */
export function coverageFromMessage(msg, options = {}) {
  if (!msg || msg.role === "user" || msg.streaming) return null;
  const state = typeof msg.kbState === "string"
    ? msg.kbState
    : (typeof msg.kb_state === "string" ? msg.kb_state : "");
  if (!KNOWN_KB_STATES.has(state)) return null;

  const hits = Array.isArray(msg.kbHits)
    ? msg.kbHits
    : (Array.isArray(msg.kb_hits) ? msg.kb_hits : []);
  const citations = Array.isArray(msg.citations) ? msg.citations : [];

  const searchedKeys = new Set();
  hits.forEach((hit, index) => {
    searchedKeys.add(hitDocumentKey(hit, index));
  });

  const citedKeys = new Set();
  const text = visibleAnswerText(msg);
  for (const match of text.matchAll(/\[(\d+)\]/g)) {
    const n = parseInt(match[1], 10);
    if (!Number.isFinite(n) || n < 1) continue;
    const key = citationDocumentKey(citations[n - 1]);
    if (key) citedKeys.add(key);
  }

  const excerpted = excerptedFilenames(hits);
  const attachmentsOmitted = options.attachmentsOmitted === true;
  const searched = searchedKeys.size;
  // 留下的命中會整批注入，沒有另一組「送進去」的清單。
  const sent = searched;
  return {
    searched,
    sent,
    cited: citedKeys.size,
    excerpted,
    line: formatCoverageLine({
      searched,
      sent,
      cited: citedKeys.size,
      excerpted,
      attachmentsOmitted,
      tools: toolNames(msg),
    }),
  };
}
