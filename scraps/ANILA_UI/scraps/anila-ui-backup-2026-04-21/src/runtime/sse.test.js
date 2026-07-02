import { describe, expect, it } from "vitest";

import { parseSseBlocks, parseSseEvent } from "./sse.js";

describe("sse parser", () => {
  it("parses mixed OpenAI and ANILA events", () => {
    const input = [
      "event: anila.trace",
      'data: {"label":"Router 分析意圖中"}',
      "",
      'data: {"choices":[{"index":0,"delta":{"content":"Hello"},"finish_reason":null}]}',
      "",
      "event: anila.meta",
      'data: {"trace_id":"trace-1","trace":[],"citations":[],"confidence":null,"handoff_chain":[],"follow_ups":[],"latency_ms":12,"classified":false}',
      "",
      "",
    ].join("\n");

    const { events, remainder } = parseSseBlocks(input);
    expect(remainder).toBe("");
    expect(events).toHaveLength(3);
    expect(events[0].event).toBe("anila.trace");
    expect(events[1].event).toBe("message");
    expect(events[2].event).toBe("anila.meta");
  });

  it("parses single event blocks", () => {
    expect(parseSseEvent("data: [DONE]")).toEqual({
      event: "message",
      data: "[DONE]",
      raw: "data: [DONE]",
    });
  });
});
