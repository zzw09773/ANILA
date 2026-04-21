import { describe, it, expect } from "vitest";
import { parseSseBlocks, parseSseEvent } from "../runtime/sse.js";

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
