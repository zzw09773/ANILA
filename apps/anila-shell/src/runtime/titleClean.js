// Sanitize raw LLM output intended as a conversation title.
//
// The title-generator LLM occasionally echoes back the assistant's placeholder
// (e.g. Router's "（Router 已完成分析但未能自動萃取…" fallback) instead of
// producing a genuine summary. Persisting that placeholder wrecks the sidebar
// because every conversation routed through the fallback ends up with the
// same title. This helper filters out known-bad outputs and asks the caller
// to fall back to the user's own first message.

// Phrases that indicate the "title" is actually quoted text from the
// assistant's failure placeholder. Keep this conservative — if more
// placeholders appear in the Router, add them here rather than in a grep of
// arbitrary assistant output (we still want legitimate summaries that
// happen to mention e.g. "分析").
const PLACEHOLDER_SIGNATURES = [
  "Router 已完成分析",
  "未能自動萃取",
  "請展開上方",
  "思考過程",
];

const TRAILING_PUNCT = /[。．.!！?？,、\s]+$/;
const LEAD_QUOTE = /^["「『『](.+)["」』』]$/s;
const MIN_TITLE_CHARS = 2;
const MAX_TITLE_CHARS = 30;

/**
 * Clean a raw title-generator output. Returns null when the output looks
 * like a failure echo and the caller should fall back to the user's text.
 *
 * @param {string} raw - the LLM's raw response
 * @returns {string | null}
 */
/**
 * Collapse self-repeating strings. Some title-generator LLMs echo the
 * answer twice ("軍人規定軍人規定" or "軍人規定 軍人規定"); we detect
 * that by checking if the first half of a stripped-whitespace string
 * equals the second half, and keep only the first copy.
 */
function dedupeRepeat(s) {
  const stripped = s.replace(/\s+/g, "");
  const n = stripped.length;
  if (n < 4 || n % 2 !== 0) return s;
  const half = n / 2;
  if (stripped.slice(0, half) === stripped.slice(half)) {
    return stripped.slice(0, half);
  }
  return s;
}

export function cleanGeneratedTitle(raw) {
  if (typeof raw !== "string") return null;
  let t = raw.trim();
  if (!t) return null;

  // Peel trailing punctuation first so `「foo」。` can still unquote.
  t = t.replace(TRAILING_PUNCT, "").trim();
  const quoted = t.match(LEAD_QUOTE);
  if (quoted) {
    t = quoted[1].replace(TRAILING_PUNCT, "").trim();
  }

  t = dedupeRepeat(t);

  if (t.length < MIN_TITLE_CHARS) return null;

  for (const sig of PLACEHOLDER_SIGNATURES) {
    if (t.includes(sig)) return null;
  }

  return t.slice(0, MAX_TITLE_CHARS);
}

export const __internals = {
  PLACEHOLDER_SIGNATURES,
  MIN_TITLE_CHARS,
  MAX_TITLE_CHARS,
};
