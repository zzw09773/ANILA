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

async function readErrorMessage(response, fallback) {
  const body = await response.text();
  console.error(`[ANILA SSE] HTTP ${response.status} response body:`, body);
  if (!body) return fallback;

  try {
    const payload = JSON.parse(body);
    if (typeof payload?.detail === "string" && payload.detail.trim()) {
      return payload.detail;
    }
    // A JSON error without the API's detail field has no user-facing message.
    // Keep the existing transport fallback instead of surfacing the envelope.
    return fallback;
  } catch {
    // Non-JSON responses (including proxy HTML) are diagnostic data only.
    return fallback;
  }
}

function statusFallback(label, status) {
  return `${label}（HTTP ${status}）`;
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
 *   onError({ message })                          — terminal mid-stream
 *     failure (anila.error). Already-streamed text stays in the
 *     accumulator; the stream then rejects with that message.
 *   onCompact({ summary, kept_from_index, method, tokens_before,
 *     tokens_after })                             — Router auto-compact
 *     boundary; persist only when method==="summary".
 *
 * All new callbacks are optional; unknown event names fall through
 * to a debug log so future server-side additions surface visibly
 * during development.
 */
export async function streamChatCompletion({
  url,
  payload,
  conversationId,
  // Slice 2b-D 最小 Task 流:對話已綁 Task 時每次 /v1 chat 呼叫都帶
  // X-ANILA-Task-Id,讓 CSP 把這次派發掛回同一個 Task。null/undefined
  // (任務建立失敗的降級模式)則完全不送此標頭。
  taskId,
  // 「改用院內規章重查」:這一次呼叫強制檢索院內規章,不看 Router 判斷。
  // ⚠ 這是一個**真的參數**,不是串進 user 訊息的一句請託。既有的 guided
  // regenerate 四個選項走的是後者(steer 文字),那條路對這件事行不通——
  // 檢索發生在 CSP,而 CSP 只看標頭;寫在訊息裡的話模型看得到、CSP 看不到,
  // 使用者會得到一個「按了、沒報錯、規章沒被查」的按鈕。
  forceKbSearch = false,
  onText,
  onTrace,
  onMeta,
  onJson,
  onReasoning,
  onCompact,
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
  onError,
  // Continue Response:回應被 max_tokens 截斷(finish_reason==='length')時回報。
  onFinishReason,
  // 思考用完輸出額度：Router 關掉思考再整理一次答案。
  onRescue,
  // 階段標題。{index, title, status}，同一則訊息上一條清單。
  onThinkingStage,
  // Stop generation:呼叫端傳入 AbortController.signal;abort() 即中止串流。
  // 已累積文字保留(onText 已即時寫入),中止不視為錯誤(回傳累積值)。
  signal,
}) {
  // Sprint 7 X follow-up：SPA 完全走 httpOnly cookie + double-submit CSRF。
  // `apiKey` parameter 已移除，避免讓呼叫端誤以為前端可以管理 key（dead
  // 路徑也是攻擊面）。SDK / curl 使用者請改打 ``apiKeyRequest`` 或自己組
  // Authorization header — 那不會經過此函式。
  const headers = { "Content-Type": "application/json" };
  if (typeof document !== "undefined") {
    const match = document.cookie.match(/(?:^|;\s*)anila_csrf=([^;]+)/);
    if (match) headers["X-CSRF-Token"] = decodeURIComponent(match[1]);
  }
  // Surface the conversation id to CSP so server-side latches
  // (memory inheritance, agent.requires_encryption) can persist
  // ``classified=true`` to the conversation row. Without this header
  // CSP can't tell which row to update, the latch silently no-ops, and
  // a hard refresh re-reads the row as un-classified — losing
  // encryption mode for the user. Sent as a header (not in payload) so
  // it doesn't get forwarded into the OpenAI-compat downstream body.
  if (typeof conversationId === "number") {
    headers["X-ANILA-Conversation-Id"] = String(conversationId);
  }
  if (taskId !== undefined && taskId !== null && taskId !== "") {
    headers["X-ANILA-Task-Id"] = String(taskId);
  }
  // 只有這一個值送得出去:Router 會把任何其他值(包含偽造的 `direct`)
  // 塌回它自己的判斷,所以前端能表達的只有「這是人按的」。沒按就完全不送,
  // 而不是送一個 `direct` —— 兩者在 Router 端等價,不送比較誠實。
  if (forceKbSearch) {
    headers["X-ANILA-Route"] = "forced";
  }
  const response = await fetch(url, {
    method: "POST",
    credentials: "include",
    headers,
    signal,
    body: JSON.stringify({ ...payload, stream: true }),
  });

  if (!response.ok) {
    const fallback = statusFallback("串流失敗", response.status);
    const detail = await readErrorMessage(response, fallback);
    const error = new Error(detail || fallback);
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
  let terminalError = null;

  while (true) {
    let chunk;
    try {
      chunk = await reader.read();
    } catch (err) {
      // 使用者按 Stop → reader.read() 拋 AbortError;吞掉並保留已累積文字。
      if (err?.name === "AbortError" || signal?.aborted) break;
      throw err;
    }
    const { done, value } = chunk;
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
        onCompact,
        onInterrupt,
        onResumed,
        onTodos,
        onFollowUps,
        onToolCallStarted,
        onToolCallFinished,
        onSpans,
        onUnknownEvent,
        onFinishReason,
        onRescue,
        onThinkingStage,
        onError: (payload) => {
          terminalError = payload;
          onError?.(payload);
        },
        accumulator: {
          get: () => accumulatedText,
          add: (delta) => {
            accumulatedText += delta;
          },
        },
      });
      if (terminalError) break;
    }
    if (terminalError) break;
  }

  if (terminalError) {
    const raw =
      typeof terminalError.message === "string"
        ? terminalError.message.trim()
        : "";
    const err = new Error(raw || "產生回應時發生錯誤，請稍後再試。");
    err.isStreamError = true;
    err.partialText = accumulatedText;
    throw err;
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
  if (event.event === "anila.thinking_stage") {
    // 模型自報的階段，或召回／救援插進同一條清單的那一步。
    safeJsonInvoke(event.data, callbacks.onThinkingStage, "anila.thinking_stage");
    return;
  }
  if (event.event === "anila.stage") {
    // Router 的 RECALL 用這個事件告訴畫面「正在搜尋過往對話」。
    // 收進同一條時間軸，不把它當成回答文字。
    safeJsonInvoke(
      event.data,
      (payload) => {
        callbacks.onTrace?.({
          at: Date.now(),
          kind: payload?.kind || "stage",
          label: payload?.label || "",
          detail: payload?.query || "",
          status:
            payload?.status === "running" ||
            payload?.status === "done" ||
            payload?.status === "error"
              ? payload.status
              : "ok",
        });
      },
      "anila.stage",
    );
    return;
  }
  if (event.event === "anila.trace") {
    // 每一步收到的時間戳：時間軸靠它算「各步耗時」與「用時 N 秒」。伺服器沒帶時間，
    // 這裡是唯一一個所有串流路徑都會經過的地方。
    safeJsonInvoke(
      event.data,
      callbacks.onTrace && ((step) => callbacks.onTrace({ at: Date.now(), ...(step || {}) })),
      "anila.trace",
    );
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
  if (event.event === "anila.compact") {
    safeJsonInvoke(event.data, callbacks.onCompact, "anila.compact");
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
  if (event.event === "anila.rescue") {
    safeJsonInvoke(event.data, callbacks.onRescue, "anila.rescue");
    return;
  }
  if (event.event === "anila.error") {
    // Terminal mid-stream failure. Do not treat the payload as an
    // OpenAI chunk; the caller keeps already-streamed text and paints
    // this message on the bubble.
    safeJsonInvoke(event.data, callbacks.onError, "anila.error");
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
  const choice = getFirstChoice(chunk);
  const text = extractChoiceText(choice);
  if (text) {
    callbacks.accumulator.add(text);
    callbacks.onText?.(callbacks.accumulator.get());
  }
  // Continue Response:回應被 max_tokens 截斷時 finish_reason==='length'。
  // 回報給呼叫端,讓 UI 決定要不要顯示「繼續」鈕(僅純文字回合)。
  const finishReason = choice?.finish_reason;
  if (finishReason) {
    callbacks.onFinishReason?.(finishReason);
  }
}


function getFirstChoice(chunk) {
  if (Array.isArray(chunk?.choices)) {
    return chunk.choices[0];
  }
  if (Array.isArray(chunk?.choice)) {
    return chunk.choice[0];
  }
  return chunk?.choice || null;
}


function extractChoiceText(choice) {
  if (!choice || typeof choice !== "object") return "";
  const delta = choice.delta && typeof choice.delta === "object" ? choice.delta : null;
  const message = choice.message && typeof choice.message === "object" ? choice.message : null;
  return (
    flattenContent(delta?.content) ||
    flattenContent(message?.content) ||
    flattenContent(choice.text)
  );
}


function flattenContent(content) {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .map((part) => {
      if (typeof part === "string") return part;
      if (part && typeof part === "object") {
        return part.text || part.content || "";
      }
      return "";
    })
    .join("");
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
  conversationId,
  callbacks = {},
  // Stop generation: same contract as streamChatCompletion. Abort keeps
  // already-streamed text and returns it instead of throwing.
  signal,
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
  // Same latch as streamChatCompletion. Resume is a new CSP turn; without
  // this header classification (and the conversation's router model) no-op.
  if (typeof conversationId === "number") {
    headers["X-ANILA-Conversation-Id"] = String(conversationId);
  }

  const url = `${(routerBaseUrl || "").replace(/\/$/, "")}/v1/sessions/${encodeURIComponent(
    sessionId,
  )}/answer`;
  const response = await fetch(url, {
    method: "POST",
    credentials: "include",
    headers,
    signal,
    body: JSON.stringify({ interrupt_id: interruptId, answer }),
  });
  if (!response.ok) {
    const fallback = statusFallback("續答失敗", response.status);
    const detail = await readErrorMessage(response, fallback);
    const error = new Error(detail || fallback);
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
  let terminalError = null;
  const accumulator = {
    get: () => accumulatedText,
    add: (delta) => {
      accumulatedText += delta;
    },
  };

  while (true) {
    let chunk;
    try {
      chunk = await reader.read();
    } catch (err) {
      if (err?.name === "AbortError" || signal?.aborted) break;
      throw err;
    }
    const { done, value } = chunk;
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parsed = parseSseBlocks(buffer);
    buffer = parsed.remainder;
    for (const event of parsed.events) {
      dispatchSseEvent(event, {
        ...callbacks,
        accumulator,
        onError: (payload) => {
          // Same terminal contract as streamChatCompletion: anila.error
          // ends the turn. Frames after it (finish=stop, [DONE]) are not
          // a successful resume.
          terminalError = payload;
          callbacks.onError?.(payload);
        },
      });
      if (terminalError) break;
    }
    if (terminalError) break;
  }

  if (terminalError) {
    const raw =
      typeof terminalError.message === "string"
        ? terminalError.message.trim()
        : "";
    const err = new Error(raw || "產生回應時發生錯誤，請稍後再試。");
    err.isStreamError = true;
    err.partialText = accumulatedText;
    throw err;
  }

  return accumulatedText;
}
