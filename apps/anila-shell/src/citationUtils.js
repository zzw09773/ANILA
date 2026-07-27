// Pure helpers for RAG citation display (anila-shell).
//
// Backend wire shape (services/csp/app/api/proxy.py `_retrieval_wire`):
//   { index, chunk_id, document_id, filename, chunk_key, excerpt, score,
//     classification_level }
// Legacy / demo shell shape still seen in some fixtures:
//   { id, title, section, snippet, score, source_uri, updated_at }
//
// Score semantics (verified against pgvector_store.similarity_search):
//   score = 1 - cosine_distance  →  cosine *similarity*, higher = closer.
//   Display as percent with score * 100 (NOT inverted). Mis-labeling this
//   as "distance" would invert trust signals.

import { CLASSIFICATION_LEVELS } from "./runtime/classified.js";

/** Honest UI label for the score axis. */
export const SCORE_LABEL = "餘弦相似度";

/** Short footnote shown once in the drawer header. */
export const SCORE_FOOTNOTE =
  "相關度＝向量餘弦相似度（1 − 餘弦距離），越高越相似；非網頁點擊率或人工評分。";

/**
 * Resolve a stable citation identity for keys / active highlight.
 * Prefer explicit id, then backend chunk_id, then positional index.
 */
export function citationIdentity(c, fallbackIndex = 0) {
  if (c == null) return `idx-${fallbackIndex}`;
  if (c.id != null && c.id !== "") return String(c.id);
  if (c.chunk_id != null && c.chunk_id !== "") return String(c.chunk_id);
  if (typeof c.index === "number") return `index-${c.index}`;
  return `idx-${fallbackIndex}`;
}

/**
 * Normalize mixed backend / legacy citation shapes into a display record.
 * Preserves the original object on `.raw` and the 1-based marker on `.n`
 * (prefer backend `index`, else array position + 1 — matches [N] markers).
 *
 * @param {object} c
 * @param {number} arrayIndex 0-based position in the citations array
 */
export function normalizeCitation(c, arrayIndex = 0) {
  const n =
    typeof c?.index === "number" && Number.isFinite(c.index) && c.index > 0
      ? c.index
      : arrayIndex + 1;
  const title =
    (typeof c?.filename === "string" && c.filename.trim()) ||
    (typeof c?.title === "string" && c.title.trim()) ||
    `來源 ${n}`;
  const section =
    (typeof c?.chunk_key === "string" && c.chunk_key.trim()) ||
    (typeof c?.section === "string" && c.section.trim()) ||
    "";
  const snippet =
    (typeof c?.excerpt === "string" && c.excerpt) ||
    (typeof c?.snippet === "string" && c.snippet) ||
    "";
  const sourceUri =
    (typeof c?.source_uri === "string" && c.source_uri.trim()) ||
    (typeof c?.url === "string" && c.url.trim()) ||
    "";
  const score = typeof c?.score === "number" && Number.isFinite(c.score) ? c.score : null;
  const classificationLevel =
    (typeof c?.classification_level === "string" && c.classification_level.trim()) ||
    (typeof c?.classificationLevel === "string" && c.classificationLevel.trim()) ||
    "";
  const documentId =
    c?.document_id != null && c.document_id !== ""
      ? c.document_id
      : c?.documentId != null && c.documentId !== ""
        ? c.documentId
        : null;

  return {
    raw: c,
    n,
    id: citationIdentity(c, arrayIndex),
    title,
    section,
    snippet,
    sourceUri,
    score,
    classificationLevel,
    documentId,
    updatedAt: typeof c?.updated_at === "string" ? c.updated_at : null,
  };
}

/**
 * Convert cosine similarity score → display percent [0, 100].
 * Clamps out-of-range values so a UI bar never lies by overflowing.
 * Returns null when score is absent.
 *
 * @param {number|null|undefined} score
 * @returns {number|null}
 */
export function scoreToPercent(score) {
  if (typeof score !== "number" || !Number.isFinite(score)) return null;
  const pct = Math.round(score * 100);
  if (pct < 0) return 0;
  if (pct > 100) return 100;
  return pct;
}

/**
 * True when this citation should sit in the URL bucket (Open WebUI-style).
 * Document-backed RAG hits (have document_id / filename from retrieval wire)
 * stay in the document group even if a source_uri is also present.
 */
export function isUrlCitation(c) {
  const uri =
    (typeof c?.source_uri === "string" && c.source_uri.trim()) ||
    (typeof c?.url === "string" && c.url.trim()) ||
    "";
  if (!uri) return false;
  const looksHttp = /^https?:\/\//i.test(uri);
  if (!looksHttp) return false;
  const hasDoc =
    (c?.document_id != null && c.document_id !== "") ||
    (c?.documentId != null && c.documentId !== "") ||
    (typeof c?.filename === "string" && c.filename.trim().length > 0);
  return !hasDoc;
}

function groupKeyFor(normalized) {
  if (normalized.documentId != null) return `doc:${normalized.documentId}`;
  // Filename-only fallback (legacy mocks without document_id).
  return `file:${normalized.title}`;
}

/**
 * Pick the highest classification among a list of zh-TW level strings,
 * using CLASSIFICATION_LEVELS order. Unknown / empty ignored.
 */
export function maxClassificationLevel(levels) {
  let best = null;
  let bestIdx = -1;
  for (const raw of levels || []) {
    if (typeof raw !== "string" || !raw.trim()) continue;
    const idx = CLASSIFICATION_LEVELS.indexOf(raw.trim());
    if (idx > bestIdx) {
      bestIdx = idx;
      best = raw.trim();
    }
  }
  return best;
}

/**
 * Group citations for the drawer:
 *   - document groups preserve first-seen order
 *   - URL sources in a trailing "url" section
 * Each item keeps its original [N] via normalizeCitation.
 *
 * @param {Array<object>|null|undefined} citations
 * @returns {{ documents: Array<{key:string,title:string,kind:'document'|'url',classificationLevel:string|null,items:ReturnType<normalizeCitation>[]}>, total: number }}
 */
export function groupCitations(citations) {
  const list = Array.isArray(citations) ? citations : [];
  const docOrder = [];
  const docMap = new Map();
  const urlItems = [];

  list.forEach((raw, i) => {
    if (isUrlCitation(raw)) {
      urlItems.push(normalizeCitation(raw, i));
      return;
    }
    const item = normalizeCitation(raw, i);
    const key = groupKeyFor(item);
    let group = docMap.get(key);
    if (!group) {
      group = {
        key,
        title: item.title,
        kind: "document",
        items: [],
      };
      docMap.set(key, group);
      docOrder.push(key);
    }
    group.items.push(item);
  });

  const documents = docOrder.map((key) => {
    const g = docMap.get(key);
    return {
      ...g,
      classificationLevel: maxClassificationLevel(
        g.items.map((it) => it.classificationLevel),
      ),
    };
  });

  if (urlItems.length > 0) {
    documents.push({
      key: "urls",
      title: "URL 來源",
      kind: "url",
      classificationLevel: maxClassificationLevel(
        urlItems.map((it) => it.classificationLevel),
      ),
      items: urlItems,
    });
  }

  return { documents, total: list.length };
}
