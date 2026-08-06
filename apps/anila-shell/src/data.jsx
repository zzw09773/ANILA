// Default folder seed + PII helpers — real agents load dynamically from CSP.

// Seed list. Users can add/remove folders at runtime (persisted in localStorage
// under "anila-folders"). `all` and `starred` are protected because the
// sidebar filter logic treats them specially (no-filter / starred-only).
//
// No demo folders are seeded beyond the two built-ins — a fresh account
// starts with a clean sidebar and the user builds their own taxonomy with
// "＋ 新增".
export const DEFAULT_FOLDERS = [
  { id: "all",     name: "全部",   icon: "inbox" },
  { id: "starred", name: "已加星", icon: "star" },
];

export const BUILTIN_FOLDER_IDS = new Set(["all", "starred"]);

// ---- PII detection patterns ----
// Front-end only. The detector has exactly two consumers: the composer hint bar
// (`warn` — tell the user what is in the draft) and the send gate (`block` —
// refuse to send it). Neither of them alters the text: what the user typed is
// what the model receives and what the conversation record keeps, byte for byte.
//
// ⚠ These four patterns are unvalidated and fire on plenty of ordinary institute
// prose (case numbers, budget columns, year lists, any official email address).
// Whether they are worth keeping in this shape is an open product question —
// widening or narrowing them is a decision, not a cleanup.
const PII_PATTERNS = [
  { kind: "id",    label: "身分證",   regex: /\b[A-Z]\d{9}\b/g },
  { kind: "phone", label: "電話",     regex: /\b09\d{2}-?\d{3}-?\d{3}\b/g },
  { kind: "email", label: "Email",    regex: /[\w.+-]+@[\w-]+\.[\w.-]+/g },
  { kind: "card",  label: "信用卡",   regex: /\b\d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}\b/g },
];

export function detectPII(text) {
  if (!text) return [];
  const hits = [];
  PII_PATTERNS.forEach(p => {
    p.regex.lastIndex = 0;
    let m;
    while ((m = p.regex.exec(text)) !== null) {
      hits.push({ kind: p.kind, label: p.label, value: m[0], index: m.index });
    }
  });
  return hits.sort((a, b) => a.index - b.index);
}

/**
 * 把偵測結果講成一句人話:`1 個身分證、2 個 Email`。
 *
 * 提示列與阻擋通知都用它。理由:「偵測到敏感資訊」只講了一個關於字串的事實,
 * 本來就知道那是什麼的人只是被拖了一秒,不知道的人什麼也沒學到。要讓人停下來
 * 想一下,得先說出**找到的是什麼**。
 *
 * @param {{label: string}[]} hits
 * @returns {string} 空陣列回空字串。
 */
export function summarizePIIHits(hits) {
  if (!hits || hits.length === 0) return "";
  const byLabel = new Map();
  hits.forEach((h) => byLabel.set(h.label, (byLabel.get(h.label) || 0) + 1));
  return [...byLabel].map(([label, n]) => `${n} 個${label}`).join("、");
}
