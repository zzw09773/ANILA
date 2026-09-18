import { describe, expect, it, vi } from "vitest";

import {
  THINKING_SUMMARY_PENDING,
  THINKING_SUMMARY_RAW_LABEL,
  appendThinkingSummary,
  createThinkingSummaryPump,
  formatThinkingElapsed,
  shouldRequestSummary,
  thinkingStatusFromFinish,
  thinkingSummaryHeadline,
  latestAssistantMessageId,
} from "../runtime/thinkingSummary.js";

describe("shouldRequestSummary", () => {
  it("waits for a useful batch, not every token", () => {
    expect(shouldRequestSummary({ addedChars: 8, elapsedMs: 200 })).toBe(false);
    expect(shouldRequestSummary({ addedChars: 80, elapsedMs: 2500 })).toBe(true);
    expect(shouldRequestSummary({ addedChars: 400, elapsedMs: 200 })).toBe(true);
    expect(shouldRequestSummary({ addedChars: 40, elapsedMs: 9000, force: true })).toBe(true);
    expect(shouldRequestSummary({ addedChars: 80, elapsedMs: 2500, inFlight: true })).toBe(false);
  });
});

describe("thinkingStatusFromFinish", () => {
  it("marks length-budget stops as aborted, not complete", () => {
    expect(thinkingStatusFromFinish({ finishReason: "stop" })).toBe("complete");
    expect(thinkingStatusFromFinish({ finishReason: "length" })).toBe("aborted");
    expect(thinkingStatusFromFinish({ lengthBudget: true, finishReason: "stop" })).toBe("aborted");
  });
});

describe("headlines", () => {
  it("uses pending copy before the first summary, never a rotating fake step", () => {
    expect(thinkingSummaryHeadline({ streaming: true, summaries: [] })).toBe(
      THINKING_SUMMARY_PENDING,
    );
    expect(thinkingSummaryHeadline({
      streaming: true,
      summaries: [{ text: "探索宇宙奧秘的提問。" }],
    })).toBe("探索宇宙奧秘的提問。");
  });

  it("formats elapsed seconds only after one second", () => {
    expect(formatThinkingElapsed(400)).toBe("0秒");
    expect(formatThinkingElapsed(5200)).toBe("5秒");
  });
});

describe("appendThinkingSummary", () => {
  it("only appends a new distinct summary", () => {
    const once = appendThinkingSummary([], "探索宇宙奧秘的提問。", 10);
    const twice = appendThinkingSummary(once, "探索宇宙奧秘的提問。", 20);
    expect(twice).toHaveLength(1);
  });
});

describe("createThinkingSummaryPump", () => {
  it("does not call the backend until a batch is ready, and fail-open keeps the stream going", async () => {
    const requestSummary = vi.fn().mockRejectedValue(new Error("csp down"));
    const onSummary = vi.fn();
    const pump = createThinkingSummaryPump({
      requestSummary,
      onSummary,
      now: () => 10_000,
    });
    pump.feed("短");
    expect(requestSummary).not.toHaveBeenCalled();
    pump.feed("x".repeat(400));
    await Promise.resolve();
    await Promise.resolve();
    expect(requestSummary).toHaveBeenCalledTimes(1);
    expect(onSummary).not.toHaveBeenCalled();
  });

  it("still flushes the last batch after close", async () => {
    const requestSummary = vi.fn().mockResolvedValue("正在整理暗物質與暗能量的差異。");
    const onSummary = vi.fn();
    const pump = createThinkingSummaryPump({
      requestSummary,
      onSummary,
      now: () => 10_000,
    });
    pump.feed("x".repeat(80));
    pump.close();
    await pump.flush();
    expect(requestSummary).toHaveBeenCalledTimes(1);
    expect(onSummary).toHaveBeenCalledWith("正在整理暗物質與暗能量的差異。");
    pump.feed("y".repeat(400));
    await Promise.resolve();
    expect(requestSummary).toHaveBeenCalledTimes(1);
  });
});

describe("latestAssistantMessageId", () => {
  it("picks the last assistant on the path", () => {
    expect(latestAssistantMessageId([
      { id: "u1", role: "user" },
      { id: "a1", role: "assistant" },
      { id: "u2", role: "user" },
      { id: "a2", role: "assistant" },
    ])).toBe("a2");
    expect(latestAssistantMessageId([{ id: "u1", role: "user" }])).toBeNull();
  });
});

describe("raw thinking is a separate entry", () => {
  it("keeps the raw-thinking label distinct from the live headline", () => {
    expect(THINKING_SUMMARY_RAW_LABEL).toBe("原始思考");
    expect(THINKING_SUMMARY_RAW_LABEL).not.toBe(THINKING_SUMMARY_PENDING);
  });
});
