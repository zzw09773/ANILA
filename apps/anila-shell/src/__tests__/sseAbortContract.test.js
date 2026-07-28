// W2-4 驗收② —— AbortError 路徑必須與現況 **bit-for-bit 一致**。
//
// `runtime/sse.js:147-149` 是這個檔案裡少數已經做對的事:使用者按 Stop 時
// reader.read() 拋 AbortError,吞掉並回傳已累積文字(中止不是錯誤)。W2-4 只動
// `!response.ok` 的錯誤映射;這支測試把 Abort 契約釘住,免得順手改壞。
//
// 這裡刻意不 mock sse.js 內部,而是餵一個真的會拋 AbortError 的 reader。

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { streamChatCompletion } from "../runtime/sse.js";

const encoder = new TextEncoder();

function chunk(text) {
  return encoder.encode(`data: ${JSON.stringify({
    choices: [{ delta: { content: text } }],
  })}\n\n`);
}

// reader:先吐 N 段文字,再拋出指定的錯誤。
function makeResponse(chunks, thrown, { ok = true, status = 200, body = "" } = {}) {
  let i = 0;
  return {
    ok,
    status,
    headers: { get: () => null },
    text: async () => body,
    body: {
      getReader: () => ({
        read: async () => {
          if (i < chunks.length) return { done: false, value: chunks[i++] };
          if (thrown) throw thrown;
          return { done: true, value: undefined };
        },
      }),
    },
  };
}

function abortError() {
  const err = new Error("The operation was aborted.");
  err.name = "AbortError";
  return err;
}

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("AbortError 契約(不得改變)", () => {
  it("mid-stream AbortError → 不 throw,回傳已累積文字", async () => {
    globalThis.fetch.mockResolvedValue(
      makeResponse([chunk("已經"), chunk("生成的一半")], abortError()),
    );
    const seen = [];
    const result = await streamChatCompletion({
      url: "/v1/chat/completions",
      payload: { model: "m" },
      onText: (acc) => seen.push(acc),
    });
    expect(result).toBe("已經生成的一半");
    expect(seen).toEqual(["已經", "已經生成的一半"]);
  });

  it("signal.aborted 為真時,任何 read 錯誤都視為中止(既有行為)", async () => {
    globalThis.fetch.mockResolvedValue(
      makeResponse([chunk("片段")], new Error("network went away")),
    );
    const controller = new AbortController();
    controller.abort();
    const result = await streamChatCompletion({
      url: "/v1/chat/completions",
      payload: { model: "m" },
      signal: controller.signal,
    });
    expect(result).toBe("片段");
  });

  it("非 Abort 的 read 錯誤(且未 abort)仍然往外拋 —— 原樣", async () => {
    const boom = new Error("network went away");
    globalThis.fetch.mockResolvedValue(makeResponse([chunk("片段")], boom));
    await expect(
      streamChatCompletion({ url: "/v1/chat/completions", payload: { model: "m" } }),
    ).rejects.toBe(boom);
  });
});

describe("!response.ok:錯誤 body 映射成使用者語彙(W2-4 ③)", () => {
  it("結構化信封 → error.code 分流,raw JSON 不進 message", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    const raw = JSON.stringify({
      error: { code: "MODEL_NOT_REGISTERED", message: "model 'x' not registered", details: null, request_id: null },
      detail: "model 'x' not registered",
    });
    globalThis.fetch.mockResolvedValue(makeResponse([], null, { ok: false, status: 400, body: raw }));

    await expect(
      streamChatCompletion({ url: "/v1/chat/completions", payload: { model: "x" } }),
    ).rejects.toMatchObject({ code: "MODEL_NOT_REGISTERED", status: 400 });

    try {
      await streamChatCompletion({ url: "/v1/chat/completions", payload: { model: "x" } });
    } catch (err) {
      expect(err.message).not.toContain("not registered");
      expect(err.message).not.toContain("{");
      expect(err.rawBody).toBe(raw);
    }
  });

  it("非 JSON body(nginx HTML)→ status fallback,HTML 不進 message", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    globalThis.fetch.mockResolvedValue(
      makeResponse([], null, { ok: false, status: 502, body: "<html>502 Bad Gateway</html>" }),
    );
    try {
      await streamChatCompletion({ url: "/v1/chat/completions", payload: { model: "x" } });
      throw new Error("should have thrown");
    } catch (err) {
      expect(err.code).toBe("SERVICE_UNAVAILABLE");
      expect(err.message).not.toContain("html");
    }
  });
});
