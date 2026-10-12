// 首頁入門卡。問句先用關鍵字從合成示範文件查出唯一一段，才可以顯示。
// 檢索單元與 packages/anila-core/.../embeddings/keyword.py 的
// cjk_search_units 同一套（tools/eval/verify_starter_prompts.py 會對過）。

import catalog from "./starterPrompts.data.json" with { type: "json" };

const CJK = /[\u3400-\u9fff]+/g;
const WORD = /[A-Za-z0-9]{2,}/g;
const MIN_SCORE = 2;

export function cjkSearchUnits(text, limit = 12) {
  const raw = String(text || "").trim();
  if (!raw) return [];
  const units = [];
  const seen = new Set();
  const add = (token) => {
    if (token && !seen.has(token) && units.length < limit) {
      seen.add(token);
      units.push(token);
    }
  };
  const runs = raw.match(CJK) || [];
  for (const run of runs) {
    if (run.length === 1) {
      add(run);
      continue;
    }
    for (const size of [2, 3]) {
      if (run.length < size) continue;
      for (let i = 0; i <= run.length - size; i += 1) {
        add(run.slice(i, i + size));
        if (units.length >= limit) return units;
      }
    }
  }
  const words = raw.match(WORD) || [];
  for (const word of words) {
    add(word.toLowerCase());
    if (units.length >= limit) break;
  }
  return units;
}

function scoreDocument(units, text) {
  const hay = String(text || "").toLowerCase();
  let score = 0;
  for (const unit of units) {
    if (hay.includes(String(unit).toLowerCase())) score += 1;
  }
  return score;
}

/**
 * 問句對文件打分。最高分至少 2、而且嚴格高於第二名，才算查到。
 * 平手不回傳：查得到哪一份說不準，就不該上架。
 */
export function retrieveStarterDocument(question, documents = catalog.documents) {
  const units = cjkSearchUnits(question);
  if (units.length === 0) return null;
  const ranked = (documents || [])
    .map((doc) => ({ doc, score: scoreDocument(units, doc?.text) }))
    .sort((a, b) => b.score - a.score || String(a.doc?.id).localeCompare(String(b.doc?.id)));
  const best = ranked[0];
  const second = ranked[1]?.score ?? 0;
  if (!best || best.score < MIN_SCORE || best.score <= second) return null;
  return { id: best.doc.id, title: best.doc.title, text: best.doc.text, score: best.score };
}

function promptIsRetrievable(prompt, documents) {
  if (!prompt?.q || !prompt.expect_doc || !prompt.expect_span || !prompt.quote) return null;
  const hit = retrieveStarterDocument(prompt.q, documents);
  if (!hit || hit.id !== prompt.expect_doc) return null;
  if (!hit.text.includes(prompt.expect_span)) return null;
  if (!hit.text.includes(prompt.quote)) return null;
  if (!prompt.quote.includes(prompt.expect_span)) return null;
  return hit;
}

export function assessStarterPrompt(prompt, documents = catalog.documents) {
  const hit = promptIsRetrievable(prompt, documents);
  return hit ? { prompt, hit } : null;
}

export function visibleStarterPrompts(data = catalog) {
  const documents = data?.documents || [];
  const out = [];
  for (const prompt of data?.prompts || []) {
    const assessed = assessStarterPrompt(prompt, documents);
    if (!assessed) continue;
    out.push({
      id: prompt.id,
      title: prompt.title,
      sub: prompt.sub,
      q: prompt.q,
      quote: prompt.quote,
      expect_span: prompt.expect_span,
      docTitle: assessed.hit.title,
    });
  }
  return out;
}

export function starterReplyForQuestion(text, data = catalog) {
  const needle = String(text || "").trim();
  if (!needle) return null;
  const visible = visibleStarterPrompts(data);
  const prompt = visible.find((item) => item.q === needle);
  if (!prompt) return null;
  const sentence = String(prompt.quote).replace(/。$/, "");
  return `這是合成範例，不是正式規章。程式在「${prompt.docTitle}」查到原文：「${sentence}」。模型沒有參與這則回答。`;
}
