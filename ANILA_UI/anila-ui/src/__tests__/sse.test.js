import { describe, it, expect, vi } from "vitest";
import {
  dispatchSseEvent,
  parseSseBlocks,
  parseSseEvent,
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
    expect(onTrace).toHaveBeenCalledWith({ kind: "thinking" });
  });

  it("routes anila.meta to onMeta", () => {
    const onMeta = vi.fn();
    dispatchSseEvent(
      { event: "anila.meta", data: '{"trace_id":"abc"}', raw: "" },
      { onMeta, accumulator: makeAccumulator() },
    );
    expect(onMeta).toHaveBeenCalledWith({ trace_id: "abc" });
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
});
