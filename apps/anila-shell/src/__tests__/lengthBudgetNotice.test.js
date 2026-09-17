import { describe, it, expect } from "vitest";
import {
  STREAM_STATE,
  canContinueLengthReply,
  isHarnessEmptyNotice,
  isLengthBudgetError,
  lengthBudgetNotice,
  streamStateNotice,
} from "../runtime/reservedTurn.js";

describe("lengthBudgetNotice", () => {
  it("does not claim leftover text when the answer is empty", () => {
    expect(lengthBudgetNotice(false)).toContain("沒有留下正文");
    expect(lengthBudgetNotice(false)).not.toContain("已產生的內容保留");
  });

  it("keeps leftover-text copy when there is content", () => {
    expect(lengthBudgetNotice(true)).toContain("已產生的內容保留");
  });

  it("still matches isLengthBudgetError after the retry phrase was dropped", () => {
    expect(
      isLengthBudgetError("LLM 回覆為空（finish_reason=length：輸出額度被思考用完）"),
    ).toBe(true);
    expect(
      isLengthBudgetError("LLM 回覆為空（finish_reason=length：輸出額度被思考用完，已重試一次）"),
    ).toBe(true);
  });
});

describe("streamStateNotice", () => {
  it("does not treat an empty complete answer as a finished reply", () => {
    expect(streamStateNotice(STREAM_STATE.COMPLETE, false)).toContain("沒有留下正文");
    expect(streamStateNotice(STREAM_STATE.COMPLETE, true)).toBeNull();
  });
});

describe("canContinueLengthReply", () => {
  it("hides Continue when the bubble is only the empty-reply notice", () => {
    const notice = "（模型沒有留下正文。可把思考調低再問，或再問一次。）";
    expect(isHarnessEmptyNotice(notice)).toBe(true);
    expect(isHarnessEmptyNotice(`${notice} ${notice}`)).toBe(true);
    expect(canContinueLengthReply({ finishReason: "length", text: notice })).toBe(false);
    expect(canContinueLengthReply({ finishReason: "length", text: `${notice}${notice}` })).toBe(false);
  });

  it("keeps Continue for a truncated real answer", () => {
    expect(canContinueLengthReply({
      finishReason: "length",
      text: "<!DOCTYPE html><html><body>太陽系",
    })).toBe(true);
  });
});
