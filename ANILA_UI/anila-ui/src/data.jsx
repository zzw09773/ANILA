// Default folder seed + PII helpers — real agents load dynamically from CSP.

// Seed list. Users can add/remove folders at runtime (persisted in localStorage
// under "anila-folders"). `all` and `starred` are protected because the
// sidebar filter logic treats them specially (no-filter / starred-only).
export const DEFAULT_FOLDERS = [
  { id: "all",       name: "全部",   icon: "inbox" },
  { id: "starred",   name: "已加星", icon: "star" },
  { id: "hr",        name: "HR",         icon: "folder" },
  { id: "finance",   name: "Finance",    icon: "folder" },
  { id: "engineering", name: "Engineering", icon: "folder" },
  { id: "compared",  name: "比較採用",   icon: "folder" },
];

export const BUILTIN_FOLDER_IDS = new Set(["all", "starred"]);

// ---- PII detection patterns (front-end UX only; real redaction at CSP proxy) ----
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

export function maskPII(value, kind) {
  if (!value) return value;
  if (kind === "email") {
    const [u, d] = value.split("@");
    return u.slice(0, 2) + "***@" + d;
  }
  if (kind === "phone") {
    return value.replace(/(\d{2,4})[^\d]?(\d{3})[^\d]?(\d{3,4})/, "$1-***-$3");
  }
  if (kind === "id") return value.slice(0, 1) + "****" + value.slice(-3);
  if (kind === "card") return "****-****-****-" + value.slice(-4);
  return "[REDACTED]";
}

export function renderWithRedaction(text, hits) {
  if (!hits || hits.length === 0) return text;
  const parts = [];
  let cursor = 0;
  hits.forEach((h, i) => {
    if (h.index > cursor) parts.push(text.slice(cursor, h.index));
    parts.push({ __redacted: true, kind: h.kind, label: h.label, masked: maskPII(h.value, h.kind), idx: i });
    cursor = h.index + h.value.length;
  });
  if (cursor < text.length) parts.push(text.slice(cursor));
  return parts;
}
