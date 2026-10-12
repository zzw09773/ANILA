// ask_user interrupt bookkeeping on an assistant message row (pending,
// submitted, merged with what the stream reports). Pure functions.

import { formatAskUserSummary, normalizeInterrupt } from "../agentic.jsx";

export function submittedInterrupt(interrupt, answer, sessionId) {
  const summary = interrupt?.kind === "ask_user"
    ? formatAskUserSummary(interrupt.payload || {}, answer)
    : interrupt?.kind === "plan"
      ? (answer?.approved === true || answer?.decision === "accept" ? "已核准計畫" : "已拒絕計畫")
      : interrupt?.kind === "tool_approval"
        ? (answer?.approved ? "已授權工具" : "已拒絕工具")
        : "已回覆";
  return normalizeInterrupt({
    ...interrupt,
    session_id: sessionId || interrupt?.session_id,
    answer,
    status: "answered",
    summary,
  });
}

export function pendingInterrupt(interrupt, sessionId) {
  return normalizeInterrupt({
    interrupt_id: interrupt?.interrupt_id,
    kind: interrupt?.kind,
    payload: interrupt?.payload,
    session_id: sessionId || interrupt?.session_id,
    status: "pending",
  });
}

// A resume that already recorded an answer must not be reopened by a
// later echo of the same interrupt. A different id is a new ASK and
// stays below the summary.
export function nextInterruptState(row, incomingRaw, sessionFallback) {
  if (!incomingRaw || typeof incomingRaw !== "object") return null;
  const current = row?.interrupt || null;
  const settled = Array.isArray(row?.settledInterrupts) ? row.settledInterrupts : [];
  const incoming = normalizeInterrupt({
    ...incomingRaw,
    session_id:
      incomingRaw.session_id ||
      current?.session_id ||
      row?.sessionId ||
      sessionFallback ||
      null,
    status: incomingRaw.status
      || ((incomingRaw.answer != null && incomingRaw.answer !== "") ? "answered" : "pending"),
  });
  if (current?.status === "answered" && current.interrupt_id === incoming.interrupt_id) {
    return { interrupt: current, settledInterrupts: settled, stacked: false };
  }
  if (current?.status === "answered" && incoming.interrupt_id !== current.interrupt_id) {
    const already = settled.some((item) => item?.interrupt_id === current.interrupt_id);
    return {
      interrupt: incoming,
      settledInterrupts: already ? settled : [...settled, current],
      stacked: true,
    };
  }
  return { interrupt: incoming, settledInterrupts: settled, stacked: false };
}
