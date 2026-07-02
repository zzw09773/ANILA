import { describe, it, expect } from "vitest";
import { buildPersistMeta } from "../runtime/messageMeta.js";

// The key invariant: streaming-accumulated trace must not be lost at the
// persistence boundary. Final anila.meta ships trace: [] by design; the
// UI message state carries the full list.

describe("buildPersistMeta - trace merge", () => {
  it("uses React state trace when final meta omits it", () => {
    const finalMeta = { trace_id: "t-1", trace: [], latency_ms: 120 };
    const state = { trace: [{ kind: "call", label: "router" }, { kind: "done" }] };
    const result = buildPersistMeta(finalMeta, state);
    expect(result.trace).toHaveLength(2);
    expect(result.trace[0].label).toBe("router");
  });

  it("keeps final meta trace when it's populated (non-stream path)", () => {
    const finalMeta = {
      trace_id: "t-2",
      trace: [{ kind: "call", label: "non-stream" }],
    };
    const state = { trace: [{ kind: "stale" }] };
    const result = buildPersistMeta(finalMeta, state);
    expect(result.trace).toEqual([{ kind: "call", label: "non-stream" }]);
  });

  it("keeps trace as [] when both sources empty but other fields exist", () => {
    const result = buildPersistMeta(
      { trace_id: "t", trace: [] },
      { trace: [] },
    );
    expect(result.trace).toEqual([]);
  });
});

describe("buildPersistMeta - reasoning merge", () => {
  it("uses accumulated reasoning when final meta is silent", () => {
    const finalMeta = { trace_id: "t" };
    const state = { reasoning: "step 1\nstep 2" };
    expect(buildPersistMeta(finalMeta, state).reasoning).toBe("step 1\nstep 2");
  });

  it("does not overwrite reasoning already provided by final meta", () => {
    const finalMeta = { reasoning: "from server" };
    const state = { reasoning: "from stream" };
    expect(buildPersistMeta(finalMeta, state).reasoning).toBe("from server");
  });
});

describe("buildPersistMeta - classified latch", () => {
  it("classified=true wins from either source", () => {
    expect(buildPersistMeta({ classified: true }, {}).classified).toBe(true);
    expect(buildPersistMeta({}, { classified: true }).classified).toBe(true);
    expect(
      buildPersistMeta({ classified: true }, { classified: false }).classified,
    ).toBe(true);
  });

  it("does not downgrade when one source says true", () => {
    expect(
      buildPersistMeta({ classified: false }, { classified: true }).classified,
    ).toBe(true);
  });

  it("leaves classified undefined/false when neither source sets it", () => {
    const result = buildPersistMeta({ trace_id: "t" }, {});
    expect(result.classified).toBeUndefined();
  });
});

describe("buildPersistMeta - handoff_chain / citations / follow_ups", () => {
  it("falls back to state handoffChain when final meta omits handoff_chain", () => {
    const result = buildPersistMeta(
      { trace_id: "t" },
      { handoffChain: [{ agent_id: "rag" }] },
    );
    expect(result.handoff_chain).toEqual([{ agent_id: "rag" }]);
  });

  it("prefers final meta handoff_chain over state handoffChain", () => {
    const result = buildPersistMeta(
      { handoff_chain: [{ agent_id: "vlm" }] },
      { handoffChain: [{ agent_id: "rag" }] },
    );
    expect(result.handoff_chain).toEqual([{ agent_id: "vlm" }]);
  });

  it("falls back to state citations / followUps", () => {
    const result = buildPersistMeta(
      {},
      { citations: [{ url: "x" }], followUps: ["q1"] },
    );
    expect(result.citations).toEqual([{ url: "x" }]);
    expect(result.follow_ups).toEqual(["q1"]);
  });
});

// ----- Sprint 13 PR B1: typed event persistence -----

describe("buildPersistMeta - Sprint 13 typed events", () => {
  it("falls back to state.todos when final meta omits todos", () => {
    const todos = [
      { id: "t1", content: "Read", status: "in_progress" },
      { id: "t2", content: "Write", status: "pending" },
    ];
    const result = buildPersistMeta({ trace_id: "t" }, { todos });
    expect(result.todos).toEqual(todos);
  });

  it("falls back to state.toolCalls list", () => {
    const toolCalls = [
      {
        tool_call_id: "tc1",
        tool_name: "exec_python",
        is_error: false,
        output_preview: "42",
      },
    ];
    const result = buildPersistMeta({ trace_id: "t" }, { toolCalls });
    expect(result.tool_calls).toEqual(toolCalls);
  });

  it("falls back to state.spans tree", () => {
    const spans = [{ id: "s1", name: "agent_run", parent_id: null }];
    const result = buildPersistMeta({ trace_id: "t" }, { spans });
    expect(result.spans).toEqual(spans);
  });

  it("persists the most recent interrupt payload", () => {
    const interrupt = {
      interrupt_id: "int-9",
      kind: "ask_user",
      payload: { question: "Pick", options: ["A", "B"] },
    };
    const result = buildPersistMeta({ trace_id: "t" }, { interrupt });
    expect(result.interrupt).toEqual(interrupt);
  });

  it("prefers final meta values when both sources carry the field", () => {
    const finalTodos = [{ id: "x", content: "from server", status: "pending" }];
    const stateTodos = [{ id: "y", content: "stale", status: "completed" }];
    const result = buildPersistMeta(
      { todos: finalTodos },
      { todos: stateTodos },
    );
    expect(result.todos).toEqual(finalTodos);
  });
});


describe("buildPersistMeta - edge cases", () => {
  it("returns null when neither source has anything useful", () => {
    expect(buildPersistMeta(null, null)).toBeNull();
    expect(buildPersistMeta(undefined, undefined)).toBeNull();
    expect(buildPersistMeta({}, {})).toBeNull();
  });

  it("tolerates non-object inputs", () => {
    expect(buildPersistMeta("not-an-object", { trace: [{ x: 1 }] })).not.toBeNull();
    expect(buildPersistMeta({ trace_id: "t" }, null).trace_id).toBe("t");
  });

  it("preserves scalar fields from final meta", () => {
    const finalMeta = {
      trace_id: "abc",
      latency_ms: 250,
      confidence: 0.9,
      classified: false,
    };
    const result = buildPersistMeta(finalMeta, { trace: [{ x: 1 }] });
    expect(result.trace_id).toBe("abc");
    expect(result.latency_ms).toBe(250);
    expect(result.confidence).toBe(0.9);
  });
});
