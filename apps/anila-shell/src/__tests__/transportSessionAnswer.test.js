// `streamSessionAnswer`(runtime/sse.js)的 transport 層。
//
// 為什麼要單獨一個檔:這個函式**自己抄了一份 CSRF 邏輯**(sse.js 裡第二
// 份、整個 shell 裡第四份)。改好 `streamChatCompletion` 那一份不會連帶
// 修好它,而 `wt/shell-reserve` 的 `csrfHeaders.test.js` 明說沒收這一處。
//
// 為什麼不是 orchestrator 測試:`app.jsx` 目前沒有任何一條路徑呼叫它
// (全樹 grep 只有 sse.js 自己的定義與兩處註解),所以掛起來的 shell 走
// 不到它 —— 只能直接呼叫。這一點寫在 scripts/mutation-check.mjs 的
// 〈無法從 harness 觸及的形狀〉裡。
//
// ⚠ 它是「已接線但還沒有呼叫端」的狀態,不是死碼:agentic.jsx 的中斷/
// 續答流程就是為它準備的。等 app.jsx 真的接上去,這裡的斷言要往上升級成
// orchestrator 級,而不是留在這個強度。

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";

import { streamSessionAnswer } from "../runtime/sse.js";

const CSRF = "session-answer-csrf-4d1b";

/** 立刻結束的 SSE 回應 —— 這裡只驗送出去的那一半。 */
function captureFetch() {
  const calls = [];
  const fake = vi.fn(async (url, options) => {
    calls.push({ url, options });
    return {
      ok: true,
      status: 200,
      headers: { get: () => null },
      text: async () => "",
      body: {
        getReader: () => ({
          read: async () => ({ done: true, value: undefined }),
          cancel: async () => {},
        }),
      },
    };
  });
  return { calls, fake };
}

let originalCookie;

beforeEach(() => {
  originalCookie = Object.getOwnPropertyDescriptor(
    Document.prototype,
    "cookie",
  );
  Object.defineProperty(document, "cookie", {
    configurable: true,
    get: () => `anila_csrf=${CSRF}`,
  });
});

afterEach(() => {
  delete document.cookie;
  if (originalCookie) Object.defineProperty(Document.prototype, "cookie", originalCookie);
  vi.restoreAllMocks();
});

describe("streamSessionAnswer — 送出去之前", () => {
  it("帶上 X-CSRF-Token(少了它每一次續答都 403)", async () => {
    const { calls, fake } = captureFetch();
    vi.stubGlobal("fetch", fake);

    await streamSessionAnswer({
      routerBaseUrl: "http://router.test",
      sessionId: "sess-1",
      interruptId: "int-1",
      answer: "使用者的回覆",
    });

    expect(calls).toHaveLength(1);
    expect(calls[0].options.headers["X-CSRF-Token"]).toBe(CSRF);
    // cookie 認證的請求必須把 cookie 送出去,否則後端當成未登入。
    expect(calls[0].options.credentials).toBe("include");
    vi.unstubAllGlobals();
  });

  it("session id 進 URL(而且做過編碼),中斷 id 與答案進 body", async () => {
    const { calls, fake } = captureFetch();
    vi.stubGlobal("fetch", fake);

    await streamSessionAnswer({
      routerBaseUrl: "http://router.test/",
      sessionId: "sess/含斜線",
      interruptId: "int-9",
      answer: "答案",
    });

    expect(calls[0].url).toBe(
      `http://router.test/v1/sessions/${encodeURIComponent("sess/含斜線")}/answer`,
    );
    expect(JSON.parse(calls[0].options.body)).toEqual({
      interrupt_id: "int-9",
      answer: "答案",
    });
    vi.unstubAllGlobals();
  });

  it("缺 sessionId / interruptId 就不發請求(而不是打一個註定 4xx 的出去)", async () => {
    const { calls, fake } = captureFetch();
    vi.stubGlobal("fetch", fake);

    await expect(
      streamSessionAnswer({ interruptId: "int-1", answer: "x" }),
    ).rejects.toThrow(/sessionId/);
    await expect(
      streamSessionAnswer({ sessionId: "sess-1", answer: "x" }),
    ).rejects.toThrow(/interruptId/);

    expect(calls).toHaveLength(0);
    vi.unstubAllGlobals();
  });
});
