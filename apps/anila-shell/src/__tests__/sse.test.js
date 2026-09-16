import { describe, it, expect, vi } from "vitest";
import {
  dispatchSseEvent,
  parseSseBlocks,
  parseSseEvent,
  streamChatCompletion,
  streamSessionAnswer,
} from "../runtime/sse.js";

describe("parseSseEvent", () => {
  it("returns null for empty block", () => {
    expect(parseSseEvent("")).toBeNull();
    expect(parseSseEvent("   \n  ")).toBeNull();
  });

  it("parses event + data lines", () => {
    const block = "event: anila.trace\ndata: {\"kind\":\"thinking\"}";
    const parsed = parseSseEvent(block);
    expect(parsed).toEqual({
      event: "anila.trace",
      data: '{"kind":"thinking"}',
      raw: block,
    });
  });

  it("defaults event name to 'message' when omitted", () => {
    const block = 'data: {"choices":[]}';
    const parsed = parseSseEvent(block);
    expect(parsed.event).toBe("message");
  });

  it("joins multi-line data with newlines", () => {
    const block = "event: note\ndata: line one\ndata: line two";
    const parsed = parseSseEvent(block);
    expect(parsed.data).toBe("line one\nline two");
  });
});

describe("parseSseBlocks", () => {
  it("returns empty when buffer is empty", () => {
    const { events, remainder } = parseSseBlocks("");
    expect(events).toEqual([]);
    expect(remainder).toBe("");
  });

  it("yields complete blocks, keeps incomplete tail as remainder", () => {
    const buffer =
      'event: anila.trace\ndata: {"k":"t"}\n\n' +
      'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n' +
      "event: anila.meta\ndata: {"; // incomplete
    const { events, remainder } = parseSseBlocks(buffer);
    expect(events).toHaveLength(2);
    expect(events[0].event).toBe("anila.trace");
    expect(events[1].event).toBe("message");
    expect(remainder.startsWith("event: anila.meta")).toBe(true);
  });

  it("normalizes CRLF line endings", () => {
    const buffer = "event: t\r\ndata: x\r\n\r\n";
    const { events, remainder } = parseSseBlocks(buffer);
    expect(events).toHaveLength(1);
    expect(remainder).toBe("");
  });

  it("treats trailing double-newline as end-of-block (no remainder)", () => {
    const buffer = 'event: anila.trace\ndata: {"k":"t"}\n\n';
    const { remainder, events } = parseSseBlocks(buffer);
    expect(events).toHaveLength(1);
    expect(remainder).toBe("");
  });

  it("preserves an incomplete trailing block without throwing", () => {
    const buffer = 'data: {"partial":';
    const { events, remainder } = parseSseBlocks(buffer);
    expect(events).toEqual([]);
    expect(remainder).toBe(buffer);
  });
});


// ---------------------------------------------------------------------
// Sprint 13 PR B1: typed event dispatch
// ---------------------------------------------------------------------


function makeAccumulator() {
  let acc = "";
  return {
    get: () => acc,
    add: (delta) => {
      acc += delta;
    },
    snapshot: () => acc,
  };
}


describe("dispatchSseEvent", () => {
  it("routes anila.trace to onTrace", () => {
    const onTrace = vi.fn();
    dispatchSseEvent(
      { event: "anila.trace", data: '{"kind":"thinking"}', raw: "" },
      { onTrace, accumulator: makeAccumulator() },
    );
    // 2026-09-02：dispatch 會把收到的時間戳 `at` 蓋上去（時間軸算各步耗時用）。
    expect(onTrace).toHaveBeenCalledWith(expect.objectContaining({ kind: "thinking" }));
    expect(typeof onTrace.mock.calls[0][0].at).toBe("number");
  });

  it("routes anila.meta to onMeta", () => {
    const onMeta = vi.fn();
    dispatchSseEvent(
      { event: "anila.meta", data: '{"trace_id":"abc"}', raw: "" },
      { onMeta, accumulator: makeAccumulator() },
    );
    expect(onMeta).toHaveBeenCalledWith({ trace_id: "abc" });
  });

  it("routes anila.compact to onCompact", () => {
    const onCompact = vi.fn();
    const payload = {
      summary: "舊回合摘要",
      kept_from_index: 4,
      method: "summary",
      tokens_before: 100,
      tokens_after: 20,
    };
    dispatchSseEvent(
      { event: "anila.compact", data: JSON.stringify(payload), raw: "" },
      { onCompact, accumulator: makeAccumulator() },
    );
    expect(onCompact).toHaveBeenCalledWith(payload);
  });

  it("extracts the delta string for anila.reasoning", () => {
    const onReasoning = vi.fn();
    dispatchSseEvent(
      { event: "anila.reasoning", data: '{"delta":"thinking..."}', raw: "" },
      { onReasoning, accumulator: makeAccumulator() },
    );
    expect(onReasoning).toHaveBeenCalledWith("thinking...");
  });

  it("ignores anila.reasoning frames without delta", () => {
    const onReasoning = vi.fn();
    dispatchSseEvent(
      { event: "anila.reasoning", data: "{}", raw: "" },
      { onReasoning, accumulator: makeAccumulator() },
    );
    expect(onReasoning).not.toHaveBeenCalled();
  });

  it("routes anila.interrupt_requested to onInterrupt", () => {
    const onInterrupt = vi.fn();
    const payload = {
      interrupt_id: "int-1",
      kind: "ask_user",
      payload: { question: "Pick", options: ["A", "B"] },
    };
    dispatchSseEvent(
      {
        event: "anila.interrupt_requested",
        data: JSON.stringify(payload),
        raw: "",
      },
      { onInterrupt, accumulator: makeAccumulator() },
    );
    expect(onInterrupt).toHaveBeenCalledWith(payload);
  });

  it("routes anila.resumed to onResumed", () => {
    const onResumed = vi.fn();
    dispatchSseEvent(
      { event: "anila.resumed", data: '{"interrupt_id":"int-9"}', raw: "" },
      { onResumed, accumulator: makeAccumulator() },
    );
    expect(onResumed).toHaveBeenCalledWith({ interrupt_id: "int-9" });
  });

  it("routes anila.todos_updated to onTodos with the full list", () => {
    const onTodos = vi.fn();
    const payload = {
      todos: [
        { id: "t1", content: "Read", status: "in_progress" },
        { id: "t2", content: "Write", status: "pending" },
      ],
    };
    dispatchSseEvent(
      {
        event: "anila.todos_updated",
        data: JSON.stringify(payload),
        raw: "",
      },
      { onTodos, accumulator: makeAccumulator() },
    );
    expect(onTodos).toHaveBeenCalledWith(payload);
  });

  it("routes anila.follow_ups to onFollowUps", () => {
    const onFollowUps = vi.fn();
    const payload = { suggestions: ["a", "b", "c"] };
    dispatchSseEvent(
      {
        event: "anila.follow_ups",
        data: JSON.stringify(payload),
        raw: "",
      },
      { onFollowUps, accumulator: makeAccumulator() },
    );
    expect(onFollowUps).toHaveBeenCalledWith(payload);
  });

  it("routes tool_call_started + tool_call_finished pair", () => {
    const onStart = vi.fn();
    const onEnd = vi.fn();
    const startPayload = {
      tool_call_id: "tc-1",
      tool_name: "exec_python",
      input: null,
    };
    const endPayload = {
      tool_call_id: "tc-1",
      tool_name: "exec_python",
      is_error: false,
      output_preview: "42",
    };
    dispatchSseEvent(
      {
        event: "anila.tool_call_started",
        data: JSON.stringify(startPayload),
        raw: "",
      },
      {
        onToolCallStarted: onStart,
        onToolCallFinished: onEnd,
        accumulator: makeAccumulator(),
      },
    );
    dispatchSseEvent(
      {
        event: "anila.tool_call_finished",
        data: JSON.stringify(endPayload),
        raw: "",
      },
      {
        onToolCallStarted: onStart,
        onToolCallFinished: onEnd,
        accumulator: makeAccumulator(),
      },
    );
    expect(onStart).toHaveBeenCalledWith(startPayload);
    expect(onEnd).toHaveBeenCalledWith(endPayload);
  });

  it("routes anila.spans to onSpans", () => {
    const onSpans = vi.fn();
    const payload = {
      spans: [{ id: "s1", name: "agent_run", parent_id: null }],
    };
    dispatchSseEvent(
      { event: "anila.spans", data: JSON.stringify(payload), raw: "" },
      { onSpans, accumulator: makeAccumulator() },
    );
    expect(onSpans).toHaveBeenCalledWith(payload);
  });

  it("falls back to onUnknownEvent for unrecognised anila.* events", () => {
    const onUnknown = vi.fn();
    dispatchSseEvent(
      { event: "anila.future_thing", data: "{}", raw: "" },
      { onUnknownEvent: onUnknown, accumulator: makeAccumulator() },
    );
    expect(onUnknown).toHaveBeenCalledWith("anila.future_thing", "{}");
  });

  it("treats unnamed events as OpenAI chunks and accumulates delta text", () => {
    const onText = vi.fn();
    const onJson = vi.fn();
    const acc = makeAccumulator();
    const callbacks = {
      onText,
      onJson,
      accumulator: acc,
    };
    dispatchSseEvent(
      {
        event: "message",
        data: '{"choices":[{"delta":{"content":"hel"}}]}',
        raw: "",
      },
      callbacks,
    );
    dispatchSseEvent(
      {
        event: "message",
        data: '{"choices":[{"delta":{"content":"lo"}}]}',
        raw: "",
      },
      callbacks,
    );
    expect(acc.snapshot()).toBe("hello");
    expect(onText).toHaveBeenLastCalledWith("hello");
    expect(onJson).toHaveBeenCalledTimes(2);
  });

  it("accepts streamed chunks that put assistant text in message.content", () => {
    const onText = vi.fn();
    const acc = makeAccumulator();
    dispatchSseEvent(
      {
        event: "message",
        data: '{"choices":[{"message":{"role":"assistant","content":"hello from agent"}}]}',
        raw: "",
      },
      { onText, accumulator: acc },
    );
    expect(acc.snapshot()).toBe("hello from agent");
    expect(onText).toHaveBeenCalledWith("hello from agent");
  });

  it("ignores [DONE] terminator", () => {
    const onText = vi.fn();
    dispatchSseEvent(
      { event: "message", data: "[DONE]", raw: "" },
      { onText, accumulator: makeAccumulator() },
    );
    expect(onText).not.toHaveBeenCalled();
  });

  it("silently drops malformed JSON for typed events", () => {
    const onTodos = vi.fn();
    expect(() =>
      dispatchSseEvent(
        {
          event: "anila.todos_updated",
          data: "{not valid json",
          raw: "",
        },
        { onTodos, accumulator: makeAccumulator() },
      ),
    ).not.toThrow();
    expect(onTodos).not.toHaveBeenCalled();
  });

  it("routes anila.error to onError and does not append text", () => {
    const onError = vi.fn();
    const onText = vi.fn();
    const acc = makeAccumulator();
    dispatchSseEvent(
      {
        event: "anila.error",
        data: JSON.stringify({
          message: "「demo」暫時無法使用，請稍後再試。",
        }),
        raw: "",
      },
      { onError, onText, accumulator: acc },
    );
    expect(onError).toHaveBeenCalledTimes(1);
    expect(onError.mock.calls[0][0].message).toContain("請稍後再試");
    expect(onText).not.toHaveBeenCalled();
    expect(acc.snapshot()).toBe("");
  });
});


describe("streamChatCompletion mid-stream anila.error", () => {
  it("preserves streamed text and rejects with the plain-language message", async () => {
    const { streamChatCompletion } = await import("../runtime/sse.js");
    const body =
      'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n' +
      'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n' +
      'event: anila.error\ndata: {"message":"「demo」暫時無法使用，請稍後再試。"}\n\n';
    const encoder = new TextEncoder();
    let pulled = false;
    const reader = {
      read: async () => {
        if (pulled) return { done: true, value: undefined };
        pulled = true;
        return { done: false, value: encoder.encode(body) };
      },
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        headers: { get: () => null },
        body: { getReader: () => reader },
      })),
    );
    const texts = [];
    const onError = vi.fn();
    await expect(
      streamChatCompletion({
        url: "/v1/chat/completions",
        payload: { model: "demo", messages: [] },
        onText: (t) => texts.push(t),
        onError,
      }),
    ).rejects.toMatchObject({
      message: "「demo」暫時無法使用，請稍後再試。",
      isStreamError: true,
      partialText: "Hello",
    });
    expect(texts.at(-1)).toBe("Hello");
    expect(onError).toHaveBeenCalledTimes(1);
    vi.unstubAllGlobals();
  });
});

describe.each([
  [
    "streamChatCompletion",
    () =>
      streamChatCompletion({
        url: "/v1/chat/completions",
        payload: { model: "demo", messages: [] },
      }),
    "串流失敗（HTTP 503）",
  ],
  [
    "streamSessionAnswer",
    () =>
      streamSessionAnswer({
        routerBaseUrl: "http://router.test",
        sessionId: "sess-1",
        interruptId: "int-1",
        answer: "答案",
      }),
    "續答失敗（HTTP 503）",
  ],
])("$0 non-OK response", (_name, invoke, fallback) => {
  it.each([
    [
      "JSON body with detail",
      '{"detail":"請前往 CSP Models 指定主路由。"}',
      "請前往 CSP Models 指定主路由。",
    ],
    [
      "JSON body without detail",
      '{"error":"upstream unavailable"}',
      fallback,
    ],
    ["plain-text body", "Bad gateway", fallback],
    ["HTML body", "<html><body>502 Bad Gateway</body></html>", fallback],
    ["empty body", "", fallback],
  ])("uses the safe message for %s", async (_shape, body, expected) => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    vi.stubGlobal("fetch", vi.fn(async () => ({
      ok: false,
      status: 503,
      text: async () => body,
    })));

    try {
      const error = await invoke().catch((err) => err);

      expect(error).toMatchObject({ message: expected, status: 503 });
      expect(consoleError).toHaveBeenCalledWith(
        "[ANILA SSE] HTTP 503 response body:",
        body,
      );
      if (body) {
        expect(error.message).not.toContain(body);
      }
      if (_shape === "JSON body with detail") {
        expect(error.message).not.toContain("\\\"");
      }
    } finally {
      consoleError.mockRestore();
      vi.unstubAllGlobals();
    }
  });
});
