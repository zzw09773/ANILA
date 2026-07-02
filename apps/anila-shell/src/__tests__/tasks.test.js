import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  createTaskForConversation,
  TASK_TITLE_MAX_LENGTH,
} from "../runtime/tasks.js";
import { streamChatCompletion } from "../runtime/sse.js";

// 最小 SSE 回應 stub:一個 [DONE] frame 後即結束,讓 streamChatCompletion
// 正常收尾,測試焦點放在送出去的 request headers。
function sseResponse() {
  const encoder = new TextEncoder();
  let sent = false;
  return {
    ok: true,
    headers: { get: () => null },
    body: {
      getReader: () => ({
        read: async () => {
          if (sent) return { done: true, value: undefined };
          sent = true;
          return { done: false, value: encoder.encode("data: [DONE]\n\n") };
        },
      }),
    },
  };
}

let warnSpy;

beforeEach(() => {
  warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("createTaskForConversation", () => {
  it("POSTs the TaskCreate contract fields and returns {taskId, traceId}", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ id: 42, trace_id: "tr-abc", status: "draft" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    const result = await createTaskForConversation({
      title: "幫我整理這份報告的重點",
      conversationId: 7,
    });

    expect(result).toEqual({ taskId: 42, traceId: "tr-abc" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toContain("/api/tasks");
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("include");
    expect(JSON.parse(init.body)).toEqual({
      title: "幫我整理這份報告的重點",
      task_type: "query",
      source_scope: "none",
      conversation_id: 7,
    });
  });

  it("omits conversation_id for non-numeric ids and truncates long titles", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ id: 1, trace_id: "tr-1" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await createTaskForConversation({
      title: "長".repeat(TASK_TITLE_MAX_LENGTH + 20),
      conversationId: "cv-local-abc",
    });

    const body = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(body.conversation_id).toBeUndefined();
    expect(body.title).toHaveLength(TASK_TITLE_MAX_LENGTH);
  });

  it("returns null and warns (zh-TW) on non-2xx", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: false, status: 503 }),
    );
    const result = await createTaskForConversation({ title: "測試" });
    expect(result).toBeNull();
    expect(warnSpy).toHaveBeenCalledWith(
      expect.stringContaining("建立任務失敗（HTTP 503）"),
    );
  });

  it("returns null and warns (zh-TW) on network error", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("failed")));
    const result = await createTaskForConversation({ title: "測試" });
    expect(result).toBeNull();
    expect(warnSpy).toHaveBeenCalledWith(
      expect.stringContaining("網路錯誤"),
      expect.any(TypeError),
    );
  });

  it("returns null when the response is missing an id", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) }),
    );
    expect(await createTaskForConversation({ title: "測試" })).toBeNull();
  });
});

// 整合層:模擬 chat 送出路徑 —— 先 createTaskForConversation,再把結果
// (taskId 或 null)交給 streamChatCompletion,驗證 X-ANILA-Task-Id 標頭
// 的有無,對應 app.jsx sendMessage 的接線方式。
describe("chat send path task header", () => {
  it("attaches X-ANILA-Task-Id when the task exists", async () => {
    const fetchMock = vi
      .fn()
      // 第一擊:POST /api/tasks
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ id: 99, trace_id: "tr-99", status: "draft" }),
      })
      // 第二擊:POST /v1/chat/completions
      .mockResolvedValueOnce(sseResponse());
    vi.stubGlobal("fetch", fetchMock);

    const task = await createTaskForConversation({ title: "你好", conversationId: 3 });
    await streamChatCompletion({
      url: "/v1/chat/completions",
      payload: { model: "anila-router", messages: [] },
      conversationId: 3,
      taskId: task?.taskId ?? null,
    });

    const chatHeaders = fetchMock.mock.calls[1][1].headers;
    expect(chatHeaders["X-ANILA-Task-Id"]).toBe("99");
    expect(chatHeaders["X-ANILA-Conversation-Id"]).toBe("3");
  });

  it("omits X-ANILA-Task-Id when task creation failed", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: false, status: 500 })
      .mockResolvedValueOnce(sseResponse());
    vi.stubGlobal("fetch", fetchMock);

    const task = await createTaskForConversation({ title: "你好", conversationId: 3 });
    expect(task).toBeNull();
    await streamChatCompletion({
      url: "/v1/chat/completions",
      payload: { model: "anila-router", messages: [] },
      conversationId: 3,
      taskId: task?.taskId ?? null,
    });

    const chatHeaders = fetchMock.mock.calls[1][1].headers;
    expect("X-ANILA-Task-Id" in chatHeaders).toBe(false);
  });
});
