/** Live thinking-summary contract — CSP produces lines, Shell displays them. */

export const THINKING_SUMMARY_PENDING = "正在思考";
export const THINKING_SUMMARY_RAW_LABEL = "原始思考";
export const MAX_THINKING_SUMMARIES = 24;

export function formatThinkingElapsed(ms) {
  if (typeof ms !== "number" || !Number.isFinite(ms) || ms < 0) return "0秒";
  return `${Math.floor(ms / 1000)}秒`;
}

export function formatThinkingComplete(ms) {
  const sec = typeof ms === "number" && ms >= 1000 ? Math.floor(ms / 1000) : 1;
  return `已思考 ${sec} 秒`;
}

export function formatThinkingAborted(ms) {
  const sec = typeof ms === "number" && ms >= 1000 ? Math.floor(ms / 1000) : 1;
  return `思考因長度上限中止 · ${sec} 秒`;
}

export function thinkingStatusFromFinish({ finishReason, lengthBudget } = {}) {
  if (lengthBudget || finishReason === "length") return "aborted";
  return "complete";
}

export function thinkingSummaryHeadline({ streaming, summaries } = {}) {
  const list = Array.isArray(summaries) ? summaries : [];
  const latest = list.length ? list[list.length - 1]?.text : "";
  if (latest) return latest;
  if (streaming) return THINKING_SUMMARY_PENDING;
  return "";
}

/** Last assistant bubble on the visible path — the only row that keeps the orb. */
export function latestAssistantMessageId(messages) {
  if (!Array.isArray(messages)) return null;
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    if (messages[i]?.role === "assistant") return messages[i].id;
  }
  return null;
}

export function appendThinkingSummary(history, text, at = 0) {
  const item = typeof text === "string" ? text.trim() : "";
  const rows = Array.isArray(history) ? [...history] : [];
  if (!item) return rows;
  if (rows.length && rows[rows.length - 1].text === item) return rows;
  rows.push({ text: item, at });
  return rows.slice(-MAX_THINKING_SUMMARIES);
}

export function shouldRequestSummary({
  addedChars = 0,
  elapsedMs = 0,
  inFlight = false,
  force = false,
} = {}) {
  if (inFlight) return false;
  if (force) return addedChars >= 40;
  if (addedChars >= 400) return true;
  return addedChars >= 80 && elapsedMs >= 2500;
}

export function createThinkingSummaryPump({
  requestSummary,
  onSummary,
  now = Date.now,
} = {}) {
  let pending = "";
  let inFlight = false;
  let lastAt = 0;
  let accepting = true;
  let current = Promise.resolve();
  const getNow = typeof now === "function" ? now : () => now;

  async function maybeFlush(force = false) {
    if (inFlight) return current;
    if (!accepting && !force) return;
    const elapsed = lastAt === 0 ? 99_999 : getNow() - lastAt;
    if (!shouldRequestSummary({
      addedChars: pending.length,
      elapsedMs: elapsed,
      inFlight,
      force,
    })) {
      return;
    }
    const added = pending;
    pending = "";
    inFlight = true;
    lastAt = getNow();
    current = (async () => {
      try {
        const summary = await requestSummary(added);
        if (summary) onSummary?.(summary);
        else pending = added + pending;
      } catch {
        pending = added + pending;
      } finally {
        inFlight = false;
      }
    })();
    return current;
  }

  return {
    feed(delta) {
      if (!accepting) return;
      if (typeof delta === "string" && delta) pending += delta;
      void maybeFlush(false);
    },
    async flush() {
      if (inFlight) await current;
      return maybeFlush(true);
    },
    close() {
      accepting = false;
    },
  };
}
