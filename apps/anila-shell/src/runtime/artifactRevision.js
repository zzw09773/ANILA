/** Decide whether a fenced artifact should replace the open preview. */

import { artifactKindFromLang, detectArtifactKind } from "./artifactDetect.js";
import { STREAM_STATE } from "./reservedTurn.js";

/**
 * @param {{
 *   streaming?: boolean,
 *   streamState?: string | null,
 *   finishReason?: string | null,
 * }} input
 */
export function canConsumeRevisionTurn(input = {}) {
  if (input.streaming) return false;
  if (input.finishReason === "length") return false;
  if (input.streamState == null || input.streamState === "") return true;
  return input.streamState === STREAM_STATE.COMPLETE;
}

/**
 * @param {{
 *   ticket?: { messageId?: string, kind?: string } | null,
 *   fenceMessageId?: string | null,
 *   streaming?: boolean,
 *   streamState?: string | null,
 *   finishReason?: string | null,
 *   artifactKind?: string,
 *   source?: string,
 *   current?: { kind?: string, source?: string } | null,
 * }} input
 * @returns {{ apply: boolean, consume: boolean }}
 */
export function shouldApplyArtifactFence(input) {
  const current = input?.current;
  const artifactKind = input?.artifactKind;
  if (!current || !artifactKind) return { apply: false, consume: false };
  if (current.kind !== artifactKind) return { apply: false, consume: false };
  const ticket = input?.ticket;
  if (ticket?.kind && ticket.kind !== artifactKind) {
    return { apply: false, consume: false };
  }
  const next = String(input?.source ?? "");
  const prev = String(current.source ?? "");
  if (next === prev) return { apply: false, consume: false };

  const fenceMessageId = input?.fenceMessageId ?? null;
  const isPrefix = Boolean(prev && next.startsWith(prev));
  if (isPrefix) {
    if (ticket?.messageId && fenceMessageId !== ticket.messageId) {
      return { apply: false, consume: false };
    }
    return { apply: true, consume: false };
  }

  if (!ticket?.messageId || !ticket?.kind) return { apply: false, consume: false };
  if (fenceMessageId !== ticket.messageId) return { apply: false, consume: false };
  if (!canConsumeRevisionTurn({
    streaming: input?.streaming,
    streamState: input?.streamState,
    finishReason: input?.finishReason,
  })) {
    return { apply: false, consume: false };
  }
  if (!next.trim()) return { apply: false, consume: false };
  return { apply: true, consume: true };
}

function isLineStart(text, index) {
  return index === 0 || text[index - 1] === "\n";
}

function backtickRun(text, index) {
  let n = 0;
  while (text[index + n] === "`") n += 1;
  return n;
}

function lineTail(text, index) {
  const nl = text.indexOf("\n", index);
  return text.slice(index, nl === -1 ? text.length : nl).replace(/\r$/, "");
}

function stripTrailingNewline(text) {
  return text.endsWith("\n") ? text.slice(0, -1) : text;
}

function collectOpenings(text) {
  const openings = [];
  for (let i = 0; i < text.length; i += 1) {
    if (!isLineStart(text, i) || text[i] !== "`") continue;
    const n = backtickRun(text, i);
    if (n < 3) continue;
    const tail = lineTail(text, i + n);
    const lang = tail.trim().split(/\s+/)[0] || "";
    let bodyStart = i + n + tail.length;
    if (text[bodyStart] === "\n") bodyStart += 1;
    openings.push({ n, lang, bodyStart });
    i += n - 1;
  }
  return openings;
}

function closeFence(text, open, greedy) {
  let lastCloser = -1;
  for (let j = open.bodyStart; j < text.length; j += 1) {
    if (!isLineStart(text, j) || text[j] !== "`") continue;
    const m = backtickRun(text, j);
    if (m < 3) continue;
    const rest = lineTail(text, j + m).trim();
    if (rest === "" && m >= open.n) {
      if (!greedy) {
        return stripTrailingNewline(text.slice(open.bodyStart, j));
      }
      lastCloser = j;
      j += m - 1;
      continue;
    }
    if (rest && m >= open.n) {
      const innerKind = artifactKindFromLang(rest.split(/\s+/)[0] || "");
      // Same-or-higher artifact fence = next product, not this closer.
      // Unclosed first fence must not borrow a later closer (standard
      // would otherwise swallow prose + the sibling). Nested shorter
      // fences (m < open.n, e.g. ```js / ```svg inside ````markdown)
      // stay inside the body.
      if (innerKind) {
        if (greedy && lastCloser >= 0) break;
        return null;
      }
    }
  }
  if (greedy && lastCloser >= 0) {
    return stripTrailingNewline(text.slice(open.bodyStart, lastCloser));
  }
  return null;
}

/**
 * Pull the best same-kind artifact fence from a raw assistant reply.
 * Nested shorter fences (```js inside ```markdown) stay inside the body.
 *
 * @param {string} text
 * @param {string} ticketKind
 * @returns {{ language: string, source: string, kind: string } | null}
 */
export function extractRevisionCandidate(text, ticketKind) {
  const raw = String(text ?? "");
  if (!raw || !ticketKind) return null;
  const candidates = [];
  for (const open of collectOpenings(raw)) {
    const standard = closeFence(raw, open, false);
    if (standard != null) {
      candidates.push({ language: open.lang, source: standard });
    }
    if (artifactKindFromLang(open.lang) === ticketKind) {
      const greedy = closeFence(raw, open, true);
      if (greedy != null) {
        candidates.push({ language: open.lang, source: greedy });
      }
    }
  }
  const matching = [];
  for (const c of candidates) {
    const kind = detectArtifactKind(c.language, c.source) || artifactKindFromLang(c.language);
    if (kind === ticketKind) matching.push({ ...c, kind });
  }
  if (!matching.length) return null;
  return matching.reduce((best, cur) => (
    cur.source.length > best.source.length ? cur : best
  ));
}
