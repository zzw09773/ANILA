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
