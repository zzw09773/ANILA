export function parseSseBlocks(buffer) {
  const normalized = buffer.replace(/\r\n/g, "\n");
  const blocks = normalized.split("\n\n");
  const hasTerminatingSeparator = normalized.endsWith("\n\n");
  const remainder = hasTerminatingSeparator ? "" : blocks.pop() || "";
  const events = blocks
    .map((block) => parseSseEvent(block))
    .filter(Boolean);
  return { events, remainder };
}

export function parseSseEvent(block) {
  if (!block.trim()) {
    return null;
  }

  let event = "message";
  const dataLines = [];

  for (const line of block.split("\n")) {
    if (line.startsWith("event:")) {
      event = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trimStart());
    }
  }

  const data = dataLines.join("\n");
  return { event, data, raw: block };
}

/**
 * Stream a chat completion and route SSE frames to typed callbacks.
 *
 * Sprint 13 PR B1 adds the Sprint 9-12 typed event surface:
 *
 *   onInterrupt({ interrupt_id, kind, payload })  — agent paused on
 *     ask_user / plan / tool_approval. UI should render an
 *     <InterruptCard> and stop accepting further turns until the user
 *     POSTs an answer to /v1/sessions/{id}/answer.
 *   onResumed({ interrupt_id })                   — first delta after
 *     a successful resume; UI clears the paused affordance.
 *   onTodos({ todos: [...] })                     — full task-board
 *     replacement for <TodoChecklist>.
 *   onFollowUps({ suggestions: [...] })           — chip suggestions
 *     for <FollowUpChips>.
 *   onToolCallStarted({ tool_call_id, tool_name, input? })
 *   onToolCallFinished({ tool_call_id, tool_name, is_error,
 *                        output_preview })
 *   onSpans({ spans: [...] })                     — OTel-style trace
 *     tree (PR B4) for <SpanTreeViewer>.
 *   onSessionId(sessionId)                        — raw header from
 *     the response so the caller can pin further turns.
 *
 * All new callbacks are optional; unknown event names fall through
 * to a debug log so future server-side additions surface visibly
 * during development.
 */
export async function streamChatCompletion({
  url,
  payload,
  onText,
  onTrace,
  onMeta,
  onJson,
  onReasoning,
  // Sprint 13 PR B1
  onInterrupt,
  onResumed,
  onTodos,
  onFollowUps,
  onToolCallStarted,
  onToolCallFinished,
  onSpans,
  onSessionId,
  onUnknownEvent,
}) {
  // Sprint 7 X follow-up：SPA 完全走 httpOnly cookie + double-submit CSRF。
  // `apiKey` parameter 已移除，避免讓呼叫端誤以為前端可以管理 key（dead
  // 路徑也是攻擊面）。SDK / curl 用戶請改打 ``apiKeyRequest`` 或自己組
  // Authorization header — 那不會經過此函式。
  const headers = { "Content-Type": "application/json" };
  if (typeof document !== "undefined") {
    const match = document.cookie.match(/(?:^|;\s*)anila_csrf=([^;]+)/);
    if (match) headers["X-CSRF-Token"] = decodeURIComponent(match[1]);
  }
  const response = await fetch(url, {
    method: "POST",
    credentials: "include",
    headers,
    body: JSON.stringify({ ...payload, stream: true }),
  });

  if (!response.ok) {
    const detail = await response.text();
    const error = new Error(detail || "Streaming failed");
    error.status = response.status;
    throw error;
  }

  // Surface the session id the Router echoes in X-Anila-Session-Id so
  // the caller can pin subsequent turns (resume, follow-up) without
  // relying on the chat history endpoint.
  const sessionIdHeader = response.headers.get("X-Anila-Session-Id");
  if (sessionIdHeader) {
    onSessionId?.(sessionIdHeader);
  }

  const reader = response.body?.getReader();
  if (!reader) {
    throw new Error("Readable stream unavailable");
  }

  const decoder = new TextDecoder();
  let buffer = "";
  let accumulatedText = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) {
      break;
    }
    buffer += decoder.decode(value, { stream: true });
    const parsed = parseSseBlocks(buffer);
    buffer = parsed.remainder;

    for (const event of parsed.events) {
      dispatchSseEvent(event, {
        onText,
        onTrace,
        onMeta,
        onJson,
        onReasoning,
        onInterrupt,
        onResumed,
        onTodos,
        onFollowUps,
        onToolCallStarted,
        onToolCallFinished,
        onSpans,
        onUnknownEvent,
        accumulator: {
          get: () => accumulatedText,
          add: (delta) => {
            accumulatedText += delta;
          },
        },
      });
    }
  }

  return accumulatedText;
}


/**
 * Map a single parsed SSE event to the right callback. Extracted so
 * the resume helper can reuse it without duplicating the dispatch
 * table. Exported for unit tests.
 *
 * `accumulator` is a tiny ref-like object the caller passes so this
 * function can append delta text and the caller can read the running
 * total — keeping the actual state outside this pure-ish dispatcher.
 */
export function dispatchSseEvent(event, callbacks) {
  if (event.data === "[DONE]") {
    return;
  }

  // Named anila.* events (server-sent metadata channels).
  if (event.event === "anila.trace") {
    safeJsonInvoke(event.data, callbacks.onTrace, "anila.trace");
    return;
  }
  if (event.event === "anila.meta") {
    safeJsonInvoke(event.data, callbacks.onMeta, "anila.meta");
    return;
  }
  if (event.event === "anila.reasoning") {
    safeJsonInvoke(
      event.data,
      (payload) => {
        if (payload?.delta) callbacks.onReasoning?.(payload.delta);
      },
      "anila.reasoning",
    );
    return;
  }

  // Sprint 13 PR B1: typed events forwarded by the Router (PR A1
  // already namespaced them all under anila.*).
  if (event.event === "anila.interrupt_requested") {
    safeJsonInvoke(event.data, callbacks.onInterrupt, "anila.interrupt_requested");
    return;
  }
  if (event.event === "anila.resumed") {
    safeJsonInvoke(event.data, callbacks.onResumed, "anila.resumed");
    return;
  }
  if (event.event === "anila.todos_updated") {
    safeJsonInvoke(event.data, callbacks.onTodos, "anila.todos_updated");
    return;
  }
  if (event.event === "anila.follow_ups") {
    safeJsonInvoke(event.data, callbacks.onFollowUps, "anila.follow_ups");
    return;
  }
  if (event.event === "anila.tool_call_started") {
    safeJsonInvoke(
      event.data, callbacks.onToolCallStarted, "anila.tool_call_started",
    );
    return;
  }
  if (event.event === "anila.tool_call_finished") {
    safeJsonInvoke(
      event.data, callbacks.onToolCallFinished, "anila.tool_call_finished",
    );
    return;
  }
  if (event.event === "anila.spans") {
    safeJsonInvoke(event.data, callbacks.onSpans, "anila.spans");
    return;
  }

  // Anything else under the anila.* namespace is forwarded raw so the
  // caller can decide how to handle future event types without a
  // client-side rebuild.
  if (event.event && event.event.startsWith("anila.")) {
    callbacks.onUnknownEvent?.(event.event, event.data);
    return;
  }

  // Default: an OpenAI-shaped chat completion chunk. Parse and route
  // delta text through the accumulator.
  let chunk;
  try {
    chunk = JSON.parse(event.data);
  } catch {
    return; // malformed — drop
  }
  callbacks.onJson?.(chunk);
  const delta = chunk.choices?.[0]?.delta?.content || "";
  if (delta) {
    callbacks.accumulator.add(delta);
    callbacks.onText?.(callbacks.accumulator.get());
  }
}


function safeJsonInvoke(rawData, callback, eventName) {
  if (!callback) return;
  let parsed;
  try {
    parsed = JSON.parse(rawData);
  } catch {
    // Malformed payload — silently drop. Could plumb to onUnknownEvent
    // but a typed callback expecting a shape shouldn't see garbage.
    return;
  }
  callback(parsed);
}


/**
 * Sprint 13 PR B1: stream a resume turn from POST /v1/sessions/{id}/answer.
 *
 * Same SSE envelope as streamChatCompletion — the agent's resumed
 * deltas + the typed events flow through the same dispatch table.
 * Returns the accumulated assistant text.
 */
export async function streamSessionAnswer({
  routerBaseUrl,
  sessionId,
  interruptId,
  answer,
  callbacks = {},
}) {
  if (!sessionId) {
    throw new Error("streamSessionAnswer: sessionId is required");
  }
  if (!interruptId) {
    throw new Error("streamSessionAnswer: interruptId is required");
  }

  const headers = { "Content-Type": "application/json" };
  if (typeof document !== "undefined") {
    const match = document.cookie.match(/(?:^|;\s*)anila_csrf=([^;]+)/);
    if (match) headers["X-CSRF-Token"] = decodeURIComponent(match[1]);
  }

  const url = `${(routerBaseUrl || "").replace(/\/$/, "")}/v1/sessions/${encodeURIComponent(
    sessionId,
  )}/answer`;
  const response = await fetch(url, {
    method: "POST",
    credentials: "include",
    headers,
    body: JSON.stringify({ interrupt_id: interruptId, answer }),
  });
  if (!response.ok) {
    const detail = await response.text();
    const error = new Error(detail || "Resume failed");
    error.status = response.status;
    throw error;
  }

  const reader = response.body?.getReader();
  if (!reader) {
    throw new Error("Readable stream unavailable");
  }

  const decoder = new TextDecoder();
  let buffer = "";
  let accumulatedText = "";
  const accumulator = {
    get: () => accumulatedText,
    add: (delta) => {
      accumulatedText += delta;
    },
  };

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parsed = parseSseBlocks(buffer);
    buffer = parsed.remainder;
    for (const event of parsed.events) {
      dispatchSseEvent(event, { ...callbacks, accumulator });
    }
  }

  return accumulatedText;
}
