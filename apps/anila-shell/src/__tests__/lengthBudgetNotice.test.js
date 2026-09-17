import { describe, it, expect } from "vitest";
import {
  STREAM_STATE,
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
