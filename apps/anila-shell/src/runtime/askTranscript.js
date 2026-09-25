/** 連續 ASK 的題目留在各自的中斷上。答案正文只拿掉中斷邊界上記下的那一段。 */

export function interruptQuestion(interrupt) {
  const raw = interrupt?.payload?.question;
  return typeof raw === "string" ? raw.trim() : "";
}

export function askQuestionsFromMessage(msg) {
  const settled = Array.isArray(msg?.settledInterrupts) ? msg.settledInterrupts : [];
  const questions = settled.map(interruptQuestion).filter(Boolean);
  const current = interruptQuestion(msg?.interrupt);
  if (current && !questions.includes(current)) questions.push(current);
  return questions;
}

/**
 * 舊對話沒有邊界偏移：題目是依序接在正文最前面的獨立片段。
 * 只從游標往下吃，對不上就停，不掃答案後半。
 */
export function legacyAskBoundarySpans(text, questions) {
  const source = typeof text === "string" ? text : "";
  const spans = [];
  let cursor = 0;
  for (const question of questions || []) {
    if (!question || !source.startsWith(question, cursor)) break;
    const after = cursor + question.length;
    if (after !== source.length && source[after] !== "\n") break;
    const end = source[after] === "\n" ? after + 1 : after;
    spans.push({ start: cursor, end });
    cursor = end;
  }
  return spans;
}

/** 有陣列就是這則已經記過邊界（空陣列＝沒有滲進正文的題目）。沒有這個欄位才走舊資料。 */
export function spansForMessage(msg) {
  if (Array.isArray(msg?.askContentSpans)) return msg.askContentSpans;
  if (Array.isArray(msg?.metadata?.ask_content_spans)) return msg.metadata.ask_content_spans;
  return legacyAskBoundarySpans(msg?.text || "", askQuestionsFromMessage(msg));
}

export function exciseSpans(text, spans) {
  if (typeof text !== "string" || !text) return typeof text === "string" ? text : "";
  if (!Array.isArray(spans) || spans.length === 0) return text;
  const ordered = [...spans].sort((a, b) => b.start - a.start);
  let out = text;
  for (const span of ordered) {
    const start = span?.start;
    const end = span?.end;
    if (!Number.isInteger(start) || !Number.isInteger(end)) continue;
    if (start < 0 || end > out.length || start >= end) continue;
    out = out.slice(0, start) + out.slice(end);
  }
  return out.replace(/\n{3,}/g, "\n\n").replace(/^\n+|\n+$/g, "");
}

function localizeSpans(spans, start, end) {
  const local = [];
  for (const span of spans || []) {
    const from = Math.max(span.start, start);
    const to = Math.min(span.end, end);
    if (to > from) local.push({ start: from - start, end: to - start });
  }
  return local;
}

/** 依全文上的邊界偏移，分別切前言與續文。偏移之外的同一句話留著。 */
export function visibleAskParts(msg) {
  const full = typeof msg?.text === "string" ? msg.text : "";
  const spans = spansForMessage({ ...msg, text: full });
  const preface = msg?.prefaceText;
  const resume = typeof msg?.resumeText === "string" ? msg.resumeText : "";
  if (preface == null) {
    return { preface: null, continuation: "", body: exciseSpans(full, spans) };
  }
  const joiner = preface && resume ? 1 : 0;
  const resumeAt = preface.length + joiner;
  return {
    preface: exciseSpans(preface, localizeSpans(spans, 0, preface.length)),
    continuation: exciseSpans(resume, localizeSpans(spans, resumeAt, resumeAt + resume.length)),
    body: exciseSpans(full, spans),
  };
}
