import { describe, expect, it, vi } from "vitest";

import {
  DUPLICATE_PENDING_COPY,
  OVERLOAD_REJECTION_COPY,
  applyOverloadCopy,
  isOverloadRejection,
  pendingSendKey,
  visibleQueueNotice,
} from "../runtime/pendingReply.js";
import { streamChatCompletion } from "../runtime/sse.js";

const QUOTA = "已達使用者每日 token 上限（10,000），將於 2026-10-02 00:00（台北時間）重置。";

function assertPlainWaitCopy(text) {
  expect(text).toContain("請先不要重送");
  expect(text).not.toContain("請稍後再試");
  expect(text).not.toMatch(/分鐘/);
}

describe("pending send key", () => {
  it("同一段文字去掉前後空白後是同一把鑰匙", () => {
    expect(pendingSendKey("  同樣的問題  ", [])).toBe(pendingSendKey("同樣的問題", []));
    expect(pendingSendKey("另一題", [])).not.toBe(pendingSendKey("同樣的問題", []));
  });

  it("附件不同就不是同一則", () => {
    const a = pendingSendKey("問題", [{ referenceId: "a" }]);
    const b = pendingSendKey("問題", [{ referenceId: "b" }]);
    expect(a).not.toBe(b);
  });
});

describe("overload copy", () => {
  it("固定句子不寫請稍後再試，也不估分鐘", () => {
    assertPlainWaitCopy(OVERLOAD_REJECTION_COPY);
    assertPlainWaitCopy(DUPLICATE_PENDING_COPY);
  });

  it("429、排隊逾時、過載要改寫；額度與一般斷線不要", () => {
    expect(isOverloadRejection({ status: 429, message: "busy" })).toBe(true);
    expect(isOverloadRejection({
      code: "model_queue_timeout",
      message: "排隊超過 120 秒，請稍後再試",
    })).toBe(true);
    expect(isOverloadRejection({
      code: "model_queue_unavailable",
      message: "暫時無法確認這個模型的使用人數，請稍後再試",
    })).toBe(true);
    expect(isOverloadRejection({ message: "模型過載" })).toBe(true);
    expect(isOverloadRejection({
      status: 429,
      code: "quota_exceeded",
      message: QUOTA,
    })).toBe(false);
    expect(isOverloadRejection({
      message: "「demo」暫時無法使用，請稍後再試。",
    })).toBe(false);

    const queued = applyOverloadCopy(new Error("排隊超過 120 秒，請稍後再試"));
    queued.code = "model_queue_timeout";
    applyOverloadCopy(queued);
    expect(queued.message).toBe(OVERLOAD_REJECTION_COPY);
    expect(queued.overload).toBe(true);

    const quota = applyOverloadCopy(Object.assign(new Error(QUOTA), {
      status: 429,
      code: "quota_exceeded",
    }));
    expect(quota.message).toBe(QUOTA);
    expect(quota.overload).not.toBe(true);
  });
});

describe("queue notice", () => {
  it("排隊位置留著，猜的分鐘拿掉", () => {
    const position = "目前使用人數較多，排隊中，你是第 2 位";
    expect(visibleQueueNotice(position)).toBe(position);
    expect(visibleQueueNotice("排隊中，預計還要 5 分鐘，你是第 2 位")).toBe("排隊中，你是第 2 位");
    expect(visibleQueueNotice("約 3 分鐘")).toBe("");
  });
});

describe("stream errors keep the copy contract", () => {
  it("額度 429 的原文不動", async () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    vi.stubGlobal("fetch", vi.fn(async () => ({
      ok: false,
      status: 429,
      text: async () => JSON.stringify({
        detail: { code: "quota_exceeded", message: QUOTA },
      }),
    })));
    try {
      const error = await streamChatCompletion({
        url: "/v1/chat/completions",
        payload: { model: "demo", messages: [] },
      }).catch((err) => err);
      expect(error.message).toBe(QUOTA);
      expect(error.overload).not.toBe(true);
    } finally {
      consoleError.mockRestore();
      vi.unstubAllGlobals();
    }
  });

  it("非額度 429 改成固定句子", async () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    vi.stubGlobal("fetch", vi.fn(async () => ({
      ok: false,
      status: 429,
      text: async () => JSON.stringify({ detail: "overloaded" }),
    })));
    try {
      const error = await streamChatCompletion({
        url: "/v1/chat/completions",
        payload: { model: "demo", messages: [] },
      }).catch((err) => err);
      expect(error.message).toBe(OVERLOAD_REJECTION_COPY);
      expect(error.overload).toBe(true);
      assertPlainWaitCopy(error.message);
    } finally {
      consoleError.mockRestore();
      vi.unstubAllGlobals();
    }
  });

  it("一般的請稍後再試不改寫", async () => {
    const { streamChatCompletion: stream } = await import("../runtime/sse.js");
    const body = 'event: anila.error\ndata: {"message":"「demo」暫時無法使用，請稍後再試。"}\n\n';
    const encoder = new TextEncoder();
    let pulled = false;
    vi.stubGlobal("fetch", vi.fn(async () => ({
      ok: true,
      headers: { get: () => null },
      body: {
        getReader: () => ({
          read: async () => {
            if (pulled) return { done: true, value: undefined };
            pulled = true;
            return { done: false, value: encoder.encode(body) };
          },
        }),
      },
    })));
    try {
      const error = await stream({
        url: "/v1/chat/completions",
        payload: { model: "demo", messages: [] },
      }).catch((err) => err);
      expect(error.message).toContain("請稍後再試");
      expect(error.overload).not.toBe(true);
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
