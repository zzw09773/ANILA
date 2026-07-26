// W2-4 ②④ —— 串流失敗的呈現與重試。
//
// 缺陷本體:`app.jsx` 的 catch 用 `text:` **覆蓋**已累積的回答,使用者眼前
// 生成到一半的內容瞬間變成一行「請求失敗:…」。這裡釘住:
//   ① 失敗後累積文字仍在 DOM(不被覆蓋);
//   ② 錯誤以獨立橫幅呈現,含「重試」鈕;
//   ③ 點重試 → 回呼拿到同一則訊息,而它帶的 payload 與原 payload 逐欄相同;
//   ④ 原始 body / 代碼不會裸貼到畫面。

import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, cleanup, fireEvent } from "@testing-library/react";
import React from "react";
import { MessageBubble } from "../chat.jsx";
import { buildRetryRequest } from "../runtime/streamPersistence.js";

afterEach(cleanup);

const ORIGINAL_PAYLOAD = Object.freeze({
  model: "anila-router",
  messages: [
    { role: "user", content: "幫我整理第三季的營收表" },
  ],
});

const failedAssistant = {
  id: "a1",
  role: "assistant",
  conversationId: 12,
  text: "第三季營收較上季成長，主要來自",
  streaming: false,
  error: {
    code: "SERVICE_UNAVAILABLE",
    message: "上游服務中斷，暫時無法產生回應，請稍後重試。",
    requestId: null,
  },
  retryPayload: ORIGINAL_PAYLOAD,
};

describe("失敗後已累積的文字仍在 DOM", () => {
  it("半成品文字沒有被錯誤字串覆蓋", () => {
    render(<MessageBubble msg={failedAssistant} agents={[]} />);
    expect(screen.getByText(/第三季營收較上季成長/)).toBeTruthy();
    expect(screen.queryByText(/^請求失敗/)).toBeNull();
  });

  it("錯誤以獨立橫幅呈現(role=alert),文案是使用者語彙", () => {
    render(<MessageBubble msg={failedAssistant} agents={[]} />);
    const alert = screen.getByRole("alert");
    expect(alert.textContent).toContain("上游服務中斷");
    // 原始 body / 機器碼不裸貼
    expect(alert.textContent).not.toContain("SERVICE_UNAVAILABLE");
    expect(alert.textContent).not.toContain("{");
  });

  it("沒有 error 的正常訊息不出現橫幅", () => {
    const ok = { ...failedAssistant, error: null };
    render(<MessageBubble msg={ok} agents={[]} />);
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

describe("重試鈕", () => {
  it("點擊 → onRetry 收到同一則訊息", () => {
    const onRetry = vi.fn();
    render(<MessageBubble msg={failedAssistant} agents={[]} onRetry={onRetry} />);
    fireEvent.click(screen.getByRole("button", { name: /重試/ }));
    expect(onRetry).toHaveBeenCalledTimes(1);
    expect(onRetry.mock.calls[0][0].id).toBe("a1");
  });

  it("重發的 payload 等於原 payload(逐欄相同,且不是重新組出來的另一份)", () => {
    const onRetry = vi.fn();
    render(<MessageBubble msg={failedAssistant} agents={[]} onRetry={onRetry} />);
    fireEvent.click(screen.getByRole("button", { name: /重試/ }));

    const request = buildRetryRequest(onRetry.mock.calls[0][0]);
    expect(request).not.toBeNull();
    expect(request.payload).toEqual(ORIGINAL_PAYLOAD);
    expect(request.payload).toBe(ORIGINAL_PAYLOAD);
    expect(request.convId).toBe(12);
    expect(request.assistantId).toBe("a1");
  });

  it("沒有 onRetry 時不渲染重試鈕(compare 視圖等唯讀情境)", () => {
    render(<MessageBubble msg={failedAssistant} agents={[]} />);
    expect(screen.queryByRole("button", { name: /重試/ })).toBeNull();
  });

  it("buildRetryRequest 對沒有 retryPayload 的訊息回 null(不亂送空 payload)", () => {
    expect(buildRetryRequest({ ...failedAssistant, retryPayload: null })).toBeNull();
    expect(buildRetryRequest(null)).toBeNull();
  });
});
