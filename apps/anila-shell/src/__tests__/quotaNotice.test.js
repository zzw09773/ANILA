import { describe, expect, it, vi } from "vitest";
import { isQuotaExceededMessage, quotaNotice } from "../runtime/quotaNotice.js";
import { streamChatCompletion } from "../runtime/sse.js";

const SENTENCE = "已達使用者每日 token 上限（10,000），將於 2026-10-02 00:00（台北時間）重置。";

describe("quota notice", () => {
  it("只把已達開頭的句子當成額度訊息", () => {
    expect(quotaNotice(SENTENCE)).toEqual({ heading: "用量已達上限", message: SENTENCE });
    expect(isQuotaExceededMessage("（已達這次回合上限）")).toBe(false);
    expect(quotaNotice("串流失敗（HTTP 429）")).toBeNull();
  });

  it("429 的 detail.message 會進到錯誤，而不是 HTTP 後備文案", async () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    vi.stubGlobal("fetch", vi.fn(async () => ({
      ok: false,
      status: 429,
      text: async () => JSON.stringify({
        detail: { code: "quota_exceeded", message: SENTENCE },
      }),
    })));
    try {
      const error = await streamChatCompletion({
        url: "/v1/chat/completions",
        payload: { model: "demo", messages: [] },
      }).catch((err) => err);
      expect(error.message).toBe(SENTENCE);
      expect(error.status).toBe(429);
      expect(error.message).not.toBe("串流失敗（HTTP 429）");
    } finally {
      consoleError.mockRestore();
      vi.unstubAllGlobals();
    }
  });

});
