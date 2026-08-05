// 送出去之前的那一層:transport header。
//
// 為什麼需要這一組:整個先落庫再串流的設計 —— POST /turn、PUT 回寫、
// /v1 串流 —— 全部是 unsafe method,全部靠 double-submit CSRF 過關。把
// `X-CSRF-Token` 那一行從 runtime/api.js 或 runtime/sse.js 刪掉,兩套測試
// 仍然全綠(假後端不驗 header),而在瀏覽器裡**每一次送出、編輯、重新產生
// 都會 403**。實測:帶 cookie 但不帶 header 打 POST /api/conversations
// → 403「CSRF 驗證失敗」;帶了 → 201。
//
// `X-ANILA-Conversation-Id` 同樣沒有守衛。它是伺服器端把
// classified/記憶繼承 latch 寫回對話列的唯一依據:少了它 latch 靜悄悄地
// no-op,使用者重整之後加密模式就不見了 —— 正是「靜默成功」那一類。

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";

import { authMultipart, authRequest, readCsrfCookie } from "../runtime/api.js";
import { streamChatCompletion } from "../runtime/sse.js";

const CSRF = "csrf-cookie-value-7c4d";

function setCookie(value) {
  Object.defineProperty(document, "cookie", {
    configurable: true,
    get: () => (value == null ? "" : `anila_csrf=${value}`),
  });
}

/** fetch 假貨:記下每一次呼叫的 headers,回一個空的 JSON 回應。 */
function captureFetch() {
  const calls = [];
  const fake = vi.fn(async (url, options) => {
    calls.push({ url, options });
    return {
      ok: true,
      status: 200,
      headers: new Headers({ "content-type": "application/json" }),
      json: async () => ({ id: 1 }),
      // sse.js 讀 body reader;直接給一個立刻結束的串流。
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

let restoreFetch;

beforeEach(() => {
  setCookie(CSRF);
  restoreFetch = globalThis.fetch;
});

afterEach(() => {
  globalThis.fetch = restoreFetch;
  vi.clearAllMocks();
});

describe("控制面請求的 CSRF header", () => {
  it("unsafe method 一律帶上 X-CSRF-Token", async () => {
    const { calls, fake } = captureFetch();
    globalThis.fetch = fake;
    for (const method of ["POST", "PUT", "PATCH", "DELETE"]) {
      await authRequest("/api/conversations", { method, body: "{}" });
    }
    expect(calls).toHaveLength(4);
    for (const call of calls) {
      expect(
        call.options.headers["X-CSRF-Token"],
        `${call.options.method} 沒有帶 CSRF header —— 瀏覽器裡會 403`,
      ).toBe(CSRF);
    }
  });

  it("預留一輪(POST /turn)帶得到,不然整條送出路徑都 403", async () => {
    const { calls, fake } = captureFetch();
    globalThis.fetch = fake;
    await authRequest("/api/conversations/1/turn", {
      method: "POST",
      body: JSON.stringify({ content: "問題", stream_writer: "tok" }),
    });
    expect(calls[0].options.headers["X-CSRF-Token"]).toBe(CSRF);
    expect(calls[0].options.credentials).toBe("include");
  });

  it("GET 不帶(伺服器豁免安全 method,帶了只是雜訊)", async () => {
    const { calls, fake } = captureFetch();
    globalThis.fetch = fake;
    await authRequest("/api/conversations");
    expect(calls[0].options.headers["X-CSRF-Token"]).toBeUndefined();
  });

  it("cookie 不在時不會硬塞一個假的值", async () => {
    setCookie(null);
    const { calls, fake } = captureFetch();
    globalThis.fetch = fake;
    await authRequest("/api/conversations", { method: "POST", body: "{}" });
    expect(readCsrfCookie()).toBe("");
    expect(calls[0].options.headers["X-CSRF-Token"]).toBeUndefined();
  });
});

describe("附件上傳的 CSRF header", () => {
  it("multipart 上傳也要帶 —— 它是自己組 headers 的第三條路徑", async () => {
    // authMultipart 刻意不走 buildHeaders(不能設 Content-Type,boundary 要
    // 讓瀏覽器填),所以它自己抄了一份 CSRF 邏輯 —— 也就是說 api.js 那一行
    // 修好不會連帶修好這一條。
    const { calls, fake } = captureFetch();
    globalThis.fetch = fake;
    await authMultipart("/api/attachments", new FormData());
    expect(calls[0].options.method).toBe("POST");
    expect(calls[0].options.headers["X-CSRF-Token"]).toBe(CSRF);
    // boundary 要留給瀏覽器決定 —— 自己設 Content-Type 會讓後端解不開。
    expect(calls[0].options.headers["Content-Type"]).toBeUndefined();
  });
});

describe("串流請求的 header", () => {
  it("帶上 X-CSRF-Token —— 少了它每一次送出都 403", async () => {
    const { calls, fake } = captureFetch();
    globalThis.fetch = fake;
    await streamChatCompletion({
      url: "http://csp.test/v1/chat/completions",
      payload: { model: "m", messages: [] },
      conversationId: 12,
    });
    expect(calls).toHaveLength(1);
    expect(calls[0].options.headers["X-CSRF-Token"]).toBe(CSRF);
    expect(calls[0].options.credentials).toBe("include");
  });

  it("帶上 X-ANILA-Conversation-Id —— 少了它機敏 latch 靜悄悄地 no-op", async () => {
    const { calls, fake } = captureFetch();
    globalThis.fetch = fake;
    await streamChatCompletion({
      url: "http://csp.test/v1/chat/completions",
      payload: { model: "m", messages: [] },
      conversationId: 12,
    });
    expect(calls[0].options.headers["X-ANILA-Conversation-Id"]).toBe("12");
    // 它是 header 而不是 body —— 進了 body 就會被轉發到下游 OpenAI-compat。
    expect(JSON.parse(calls[0].options.body).conversation_id).toBeUndefined();
  });

  it("沒有對話 id(未落庫的暫態對話)就不帶那個 header", async () => {
    const { calls, fake } = captureFetch();
    globalThis.fetch = fake;
    await streamChatCompletion({
      url: "http://csp.test/v1/chat/completions",
      payload: { model: "m", messages: [] },
    });
    expect(calls[0].options.headers["X-ANILA-Conversation-Id"]).toBeUndefined();
    // CSRF 仍然要帶:沒有對話 id 不代表這個請求不是 unsafe method。
    expect(calls[0].options.headers["X-CSRF-Token"]).toBe(CSRF);
  });
});
