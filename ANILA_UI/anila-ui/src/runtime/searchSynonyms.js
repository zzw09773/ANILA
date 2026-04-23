// Lightweight keyword expansion for the sidebar search.
//
// Users often search for a concept ("特休") expecting to find conversations
// about the broader topic (HR / 休假 / 假期). The sidebar's default
// title-only substring match misses these because conversation titles are
// LLM-generated and might say "年假" or "請假規定" instead.
//
// Strategy: a compact hand-curated synonym table covering the HR, Finance,
// Code, and Legal topics most often surfaced by ANILA's registered agents.
// For each token the user types we expand it into a set of equivalent terms
// and succeed if ANY of them substring-matches the conversation's title or
// one of its tags. Cheap enough to run on every keystroke against the
// in-memory conversation list.

const SYNONYM_GROUPS = [
  ["特休", "年假", "休假", "假期", "請假", "hr", "人資"],
  ["加班", "超時", "ot", "overtime", "hr", "人資"],
  ["報銷", "請款", "差旅", "出差", "finance", "財會"],
  ["發票", "收據", "報帳", "finance", "財會"],
  ["薪資", "薪水", "payroll", "finance"],
  ["sse", "streaming", "fastapi", "code", "程式"],
  ["api", "endpoint", "code", "程式"],
  ["申訴", "訴願", "陳情", "legal", "法務", "軍法"],
  ["條文", "法規", "law", "legal", "法務"],
  ["合約", "契約", "contract", "legal", "法務"],
];

// Reverse index: token → array of group indexes it belongs to. Built once.
const _tokenToGroups = (() => {
  const map = new Map();
  SYNONYM_GROUPS.forEach((group, idx) => {
    group.forEach((tok) => {
      const key = tok.toLowerCase();
      if (!map.has(key)) map.set(key, []);
      map.get(key).push(idx);
    });
  });
  return map;
})();

/**
 * Expand a single lowercased query token into every synonym it maps to
 * (including itself). Tokens not in the table return just themselves.
 */
export function expandTerm(token) {
  const key = token.toLowerCase();
  const groups = _tokenToGroups.get(key);
  if (!groups) return [key];
  const out = new Set([key]);
  for (const g of groups) {
    for (const t of SYNONYM_GROUPS[g]) out.add(t.toLowerCase());
  }
  return [...out];
}

/**
 * Return true when `query` matches `conversation`'s title or tags, with
 * synonym expansion on each whitespace-split token. All tokens must match
 * (AND) but any synonym satisfies each token (OR within the expansion).
 */
export function matchFuzzy(conversation, query) {
  const q = (query || "").trim().toLowerCase();
  if (!q) return true;
  const title = (conversation.title || "").toLowerCase();
  const tags = (conversation.tags || []).map((t) => t.toLowerCase());
  const haystack = title + " " + tags.join(" ");
  return q.split(/\s+/).every((tok) => {
    return expandTerm(tok).some((syn) => haystack.includes(syn));
  });
}
