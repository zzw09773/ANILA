// W2-3 / C3 §d:一列 assistant 上的 `dbId` **只能**代表「目前顯示的這個版本
// 對應到哪一列伺服器訊息」。它一度同時兼差當 regenerate 的 fork 來源,兩個意思
// 擠在同一欄,於是只要顯示中的版本沒有自己的 id,那一欄就會回退成**兄弟版本**
// 的 id —— 畫面是 A、身分是 B,任何以 id 為鍵的動作(評分、active-leaf)都打到
// 錯的伺服器列。
//
// 三條情境都用**真的** App 驅動(DOM 點擊),只 mock client 與 SSE:
//   PROBE-C 原始 assistant 沒存成 → regenerate 走 append → 切回版本 0 評分
//   PROBE-A 串流成功但 fork POST 失敗 → 新版本沒有 id
//   PROBE-B 串流一個字都沒吐就失敗 → 版本列仍必須可以切回去
import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const mocks = vi.hoisted(() => ({
  authRequest: vi.fn(async () => ({})),
  listConversations: vi.fn(async () => []),
  getConversation: vi.fn(async () => ({})),
  searchConversations: vi.fn(async () => []),
  setActiveLeaf: vi.fn(async () => ({})),
  forkAssistantMessage: vi.fn(async () => ({})),
  appendMessage: vi.fn(async () => ({})),
  rateMessage: vi.fn(async () => ({})),
  updateMessage: vi.fn(async () => ({})),
  streamChatCompletion: vi.fn(async () => ({})),
}));

vi.mock("../runtime/auth.jsx", () => ({
  AuthProvider: ({ children }) => children,
  useAuth: () => ({
    user: { username: "tester", email: "tester@example.test", role: "user" },
    authReady: true,
    isAuthenticated: true,
    logout: vi.fn(),
    authRequest: mocks.authRequest,
    multipartRequest: vi.fn(async () => ({})),
    getCsrfToken: () => "",
  }),
  useLogoutRedirect: () => vi.fn(),
}));

vi.mock("../runtime/conversations.js", async (importOriginal) => {
  const actual = await importOriginal();
  return {
    ...actual,
    listConversations: (...args) => mocks.listConversations(...args),
    getConversation: (...args) => mocks.getConversation(...args),
    searchConversations: (...args) => mocks.searchConversations(...args),
    setActiveLeaf: (...args) => mocks.setActiveLeaf(...args),
    forkAssistantMessage: (...args) => mocks.forkAssistantMessage(...args),
    appendMessage: (...args) => mocks.appendMessage(...args),
    rateMessage: (...args) => mocks.rateMessage(...args),
    updateMessage: (...args) => mocks.updateMessage(...args),
    getUiSettings: vi.fn(async () => ({ ui_settings: {} })),
    putUiSettings: vi.fn(async () => ({ ui_settings: {} })),
    listActiveBanners: vi.fn(async () => []),
    listAgentFunctions: vi.fn(async () => []),
  };
});

vi.mock("../runtime/sse.js", async (importOriginal) => {
  const actual = await importOriginal();
  return {
    ...actual,
    streamChatCompletion: (...args) => mocks.streamChatCompletion(...args),
  };
});

import App from "../app.jsx";
import { ConfirmProvider } from "../confirm.jsx";

// jsdom 沒有 Element.prototype.scrollTo(訊息區 autoscroll 會呼叫)。
if (typeof Element !== "undefined" && !Element.prototype.scrollTo) {
  Element.prototype.scrollTo = function scrollToStub() {};
}

const CONV_ID = 42;
const TITLE = "版本身分";

const ROW = {
  id: CONV_ID,
  title: TITLE,
  agent_id: null,
  origin: "anila-ui",
  classified: false,
  classification_inherited: false,
  classification_level: "無機密",
  created_at: "2026-07-27T09:00:00Z",
  updated_at: "2026-07-27T10:00:00Z",
};

function msg(id, role, content, parentId, seconds) {
  return {
    id,
    role,
    content,
    parent_id: parentId,
    metadata: {},
    created_at: `2026-07-27T10:00:0${seconds}Z`,
  };
}

/** 線性樹:一問一答,assistant 已持久化(dbId 2)。regenerate 會 fork 它。 */
function linearDetail() {
  return {
    ...ROW,
    active_leaf_message_id: 2,
    messages: [
      msg(1, "user", "問題", null, 1),
      msg(2, "assistant", "答案一", 1, 2),
    ],
  };
}

/** 空對話:訊息由測試自己從 composer 送出,好複製「assistant 沒存成」。 */
function emptyDetail() {
  return { ...ROW, active_leaf_message_id: null, messages: [] };
}

async function openConversation() {
  render(<ConfirmProvider><App /></ConfirmProvider>);
  const rows = await screen.findAllByText(TITLE);
  fireEvent.click(rows[0]);
}

async function regenerate() {
  fireEvent.click(await screen.findByTitle("重新產生（可選調整方向）"));
  fireEvent.click(await screen.findByText("重試（不調整）"));
}

async function sendFromComposer(text) {
  const box = await screen.findByPlaceholderText(/問 ANILA 任何事情/);
  fireEvent.change(box, { target: { value: text } });
  fireEvent.click(screen.getByLabelText("送出"));
}

beforeEach(() => {
  mocks.authRequest.mockReset().mockResolvedValue({});
  mocks.listConversations.mockReset().mockResolvedValue([ROW]);
  mocks.getConversation.mockReset().mockResolvedValue(linearDetail());
  mocks.searchConversations.mockReset().mockResolvedValue([]);
  mocks.setActiveLeaf.mockReset().mockResolvedValue({});
  mocks.forkAssistantMessage.mockReset().mockResolvedValue({});
  mocks.appendMessage.mockReset().mockResolvedValue({});
  mocks.rateMessage.mockReset().mockResolvedValue({});
  mocks.updateMessage.mockReset().mockResolvedValue({});
  mocks.streamChatCompletion.mockReset().mockResolvedValue({});
  vi.stubGlobal("fetch", vi.fn(async () => ({
    ok: true, status: 200, json: async () => ({ data: [] }),
  })));
  window.sessionStorage?.clear?.();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("PROBE-C:原始 assistant 沒存成 → regenerate 走 append", () => {
  it("切回版本 0 後評分,打的不是版本 1 的伺服器列", async () => {
    mocks.getConversation.mockResolvedValue(emptyDetail());
    // user 存得成;assistant 第一次存失敗(→ dbId undefined),
    // regenerate 那次 append 才成功並拿到 77。
    mocks.appendMessage.mockImplementation(async (_req, _conv, body) => {
      if (body?.role === "user") return { id: 10 };
      if (mocks.appendMessage.mock.calls.filter((c) => c[2]?.role === "assistant").length === 1) {
        throw new Error("assistant 落地失敗");
      }
      return { id: 77 };
    });
    mocks.streamChatCompletion.mockImplementationOnce(async (opts) => {
      opts.onText?.("原始答案");
      return {};
    });

    await openConversation();
    await sendFromComposer("問題");
    expect(await screen.findByText("原始答案")).toBeInTheDocument();

    mocks.streamChatCompletion.mockImplementation(async (opts) => {
      opts.onText?.("重生的答案");
      return {};
    });
    await regenerate();
    expect(await screen.findByText("重生的答案")).toBeInTheDocument();
    await screen.findByText("2 / 2");

    // fork 沒被用到(原訊息從未持久化),走的是 append 分支並拿到 77。
    expect(mocks.forkAssistantMessage).not.toHaveBeenCalled();
    await waitFor(() =>
      expect(
        mocks.appendMessage.mock.calls.filter((c) => c[2]?.role === "assistant"),
      ).toHaveLength(2),
    );

    // 切回版本 0 —— 它有文字所以動作列會渲染,👍 點得下去。
    fireEvent.click(screen.getByTitle("上一個版本"));
    expect(await screen.findByText("原始答案")).toBeInTheDocument();
    await screen.findByText("1 / 2");

    fireEvent.click(screen.getByTitle("標記為有用"));

    // ⚠ 迴歸點:版本 0 從來沒被存過,77 是**版本 1** 的列。使用者對著版本 0
    // 按讚,評分卻落到另一個版本上。寧可不送(顯示「尚未儲存」),也不能送 77。
    await new Promise((r) => setTimeout(r, 0));
    expect(mocks.rateMessage.mock.calls.map((c) => c[2])).not.toContain(77);
    expect(mocks.rateMessage).not.toHaveBeenCalled();
  });
});

describe("PROBE-A:串流成功但 fork POST 失敗", () => {
  it("新版本沒有 id → 不 PUT 前一個兄弟的 id,評分也不打到它", async () => {
    mocks.streamChatCompletion.mockImplementation(async (opts) => {
      opts.onText?.("重生的答案");
      opts.onMeta?.({ trace_id: "trace-1", latency_ms: 10 });
      return {};
    });
    mocks.forkAssistantMessage.mockRejectedValue(new Error("fork 失敗"));

    await openConversation();
    expect(await screen.findByText("答案一")).toBeInTheDocument();
    await regenerate();

    expect(await screen.findByText("重生的答案")).toBeInTheDocument();
    await screen.findByText("2 / 2");
    await waitFor(() => expect(mocks.forkAssistantMessage).toHaveBeenCalledTimes(1));

    // ① 對著沒存成的新版本按讚 —— 不可以打到前一個兄弟(2)。
    fireEvent.click(screen.getByTitle("標記為有用"));
    await new Promise((r) => setTimeout(r, 0));
    expect(mocks.rateMessage.mock.calls.map((c) => c[2])).not.toContain(2);
    expect(mocks.rateMessage).not.toHaveBeenCalled();

    // ② 切到版本 0(它就是訊息 2)→ 這一次 PUT 2 是對的。
    fireEvent.click(screen.getByTitle("上一個版本"));
    expect(await screen.findByText("答案一")).toBeInTheDocument();
    await waitFor(() => expect(mocks.setActiveLeaf).toHaveBeenCalledTimes(1));
    expect(mocks.setActiveLeaf).toHaveBeenLastCalledWith(mocks.authRequest, CONV_ID, 2);

    // ③ 再切回沒存成的版本 1 → 伺服器端沒有葉可以指,不可以再送 2。
    fireEvent.click(screen.getByTitle("下一個版本"));
    expect(await screen.findByText("重生的答案")).toBeInTheDocument();
    await screen.findByText("2 / 2");
    await new Promise((r) => setTimeout(r, 0));
    expect(mocks.setActiveLeaf).toHaveBeenCalledTimes(1);
  });
});

describe("PROBE-B:串流一個字都沒吐就失敗", () => {
  it("失敗的版本仍切得回去,不會把使用者鎖在空泡泡裡", async () => {
    mocks.streamChatCompletion.mockImplementation(async () => {
      throw new Error("連線中斷");
    });

    await openConversation();
    expect(await screen.findByText("答案一")).toBeInTheDocument();
    await regenerate();

    // 失敗橫幅出得來(既有行為)。
    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());

    // ⚠ 迴歸點:版本列被 `msg.text` 這個閘門連坐藏掉,使用者只剩重整一途。
    const prev = await screen.findByTitle("上一個版本");
    fireEvent.click(prev);
    expect(await screen.findByText("答案一")).toBeInTheDocument();
  });
});
