// W2-3 / C3 §d:版本切換與 regenerate fork 的**整合層**行為。
//
// 為什麼要在 App 這一層測:`applyRevisionSwitch` 回傳 `{ nextList,
// activeLeafId }`,但 `app.jsx` 的 `switchRevision` 曾經只取 `nextList`、自己
// 再算一次要持久化的葉。兩邊在「該版本還沒被持久化」時給出不同答案 ——
// 模組回傳前一個兄弟的 dbId(陳舊指標),元件算出 null(跳過 PUT)。單測釘的
// 是模組那份、production 送的是元件那份,兩邊都沒被真正驗到。
//
// 這三條測試一律驅動**真的** `switchRevision` / `regenerateMessage`(從 DOM
// 上的 < N/M > pager 與重新產生選單點下去),client 才是 mock。
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
  streamChatCompletion: vi.fn(async () => ({})),
}));

vi.mock("../runtime/auth.jsx", () => ({
  AuthProvider: ({ children }) => children,
  useAuth: () => ({
    user: { username: "tester", email: "tester@ncsist.org.tw", role: "user" },
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
const TITLE = "版本切換";

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

/**
 * 分枝樹:a1(2) 底下還有 follow(4) → reply(5);a2(3) 是 a1 的兄弟、目前的
 * active leaf。切回 a1 這一枝時,要持久化的是該枝**最深**的節點 5,不是 2。
 */
function branchedDetail() {
  return {
    ...ROW,
    active_leaf_message_id: 3,
    messages: [
      msg(1, "user", "問題", null, 1),
      msg(2, "assistant", "答案一", 1, 2),
      msg(3, "assistant", "答案二", 1, 3),
      msg(4, "user", "追問", 2, 4),
      msg(5, "assistant", "追答", 4, 5),
    ],
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

async function openConversation() {
  render(<ConfirmProvider><App /></ConfirmProvider>);
  const rows = await screen.findAllByText(TITLE);
  fireEvent.click(rows[0]);
}

async function regenerate() {
  fireEvent.click(await screen.findByTitle("重新產生（可選調整方向）"));
  fireEvent.click(await screen.findByText("重試（不調整）"));
}

beforeEach(() => {
  mocks.authRequest.mockReset().mockResolvedValue({});
  mocks.listConversations.mockReset().mockResolvedValue([ROW]);
  mocks.getConversation.mockReset().mockResolvedValue(branchedDetail());
  mocks.searchConversations.mockReset().mockResolvedValue([]);
  mocks.setActiveLeaf.mockReset().mockResolvedValue({});
  mocks.forkAssistantMessage.mockReset().mockResolvedValue({});
  mocks.appendMessage.mockReset().mockResolvedValue({});
  mocks.streamChatCompletion.mockReset().mockResolvedValue({});
  vi.stubGlobal("fetch", vi.fn(async () => ({
    ok: true, status: 200, json: async () => ({ data: [] }),
  })));
  window.sessionStorage?.clear?.();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("switchRevision 持久化的葉(真 App,非 stub)", () => {
  it("切到另一個分枝 → PUT 的是該枝最深的節點,不是被切到的那一則本身", async () => {
    await openConversation();
    // 目前在 a2(3);它沒有子孫,pager 顯示 2 / 2。
    expect(await screen.findByText("答案二")).toBeInTheDocument();
    await screen.findByText("2 / 2");

    fireEvent.click(screen.getByTitle("上一個版本"));

    // 畫面換成 a1 這一枝,含它底下的追問/追答。
    expect(await screen.findByText("答案一")).toBeInTheDocument();
    await screen.findByText("追答");

    await waitFor(() => expect(mocks.setActiveLeaf).toHaveBeenCalledTimes(1));
    // ⚠ 5(該枝最深的葉),不是 2(切到的那一則)。指到 2 的話重新整理後
    // 追問/追答就從 active path 消失。
    expect(mocks.setActiveLeaf).toHaveBeenCalledWith(mocks.authRequest, CONV_ID, 5);
  });

  it("切回一個從未被持久化的版本 → 刻意不 PUT,而不是把舊兄弟的 id 當指標送出去", async () => {
    // 串流吐了一段文字才斷線 → regenerate 的新版本停在「有文字、無 dbId」:
    // 它從來沒進過後端,伺服器那邊沒有對應的葉可以指。
    mocks.getConversation.mockResolvedValue(linearDetail());
    mocks.streamChatCompletion.mockImplementation(async (opts) => {
      opts.onText?.("斷線前的半句");
      throw new Error("連線中斷");
    });

    await openConversation();
    expect(await screen.findByText("答案一")).toBeInTheDocument();
    await regenerate();

    // 兩個版本:0 = 原答案(dbId 2)、1 = 這次沒存成的重生。
    await screen.findByText("2 / 2");
    expect(mocks.forkAssistantMessage).not.toHaveBeenCalled();

    // 先切到版本 1(有 dbId)→ 會 PUT;確認 pager 本身是通的。
    fireEvent.click(screen.getByTitle("上一個版本"));
    await screen.findByText("1 / 2");
    await waitFor(() => expect(mocks.setActiveLeaf).toHaveBeenCalledTimes(1));
    expect(mocks.setActiveLeaf).toHaveBeenLastCalledWith(mocks.authRequest, CONV_ID, 2);

    // 再切回那個沒存成的版本 —— 不可以再送任何 active-leaf。
    fireEvent.click(screen.getByTitle("下一個版本"));
    // 切換確實發生了:斷線那半句只活在 top-level 鏡射欄位上,沒被 freeze 進
    // revisions[1](freeze 在 await 之後、例外把它跳過了),所以切回來是空的。
    await waitFor(() =>
      expect(screen.queryByText("斷線前的半句")).not.toBeInTheDocument(),
    );
    // 這一條才是重點:沒有第二次 PUT。舊寫法會把版本 0 的 dbId(2)當成版本
    // 1 的葉送出去,伺服器的 active leaf 就會指向使用者剛離開的那一枝。
    expect(mocks.setActiveLeaf).toHaveBeenCalledTimes(1);
  });
});

describe("regenerate → fork 的真流程(真 App,非 stub)", () => {
  it("重新產生會 fork 出兄弟,並把回傳的 id 補進該版本(之後切換用得到)", async () => {
    mocks.getConversation.mockResolvedValue(linearDetail());
    mocks.streamChatCompletion.mockImplementation(async (opts) => {
      opts.onText?.("重生後的答案");
      opts.onMeta?.({ trace_id: "trace-9", latency_ms: 123 });
      return {};
    });
    mocks.forkAssistantMessage.mockResolvedValue({ id: 99 });

    await openConversation();
    expect(await screen.findByText("答案一")).toBeInTheDocument();
    await regenerate();

    // ① client 呼叫:fork 掛在原 assistant(dbId 2)底下,帶串流結果與 meta。
    await waitFor(() => expect(mocks.forkAssistantMessage).toHaveBeenCalledTimes(1));
    const [req, convId, parentDbId, patch] = mocks.forkAssistantMessage.mock.calls[0];
    expect(req).toBe(mocks.authRequest);
    expect(convId).toBe(CONV_ID);
    expect(parentDbId).toBe(2);
    expect(patch).toMatchObject({
      content: "重生後的答案",
      traceId: "trace-9",
      latencyMs: 123,
    });
    // 沒有走「這則還沒存過」的 append 分支。
    expect(mocks.appendMessage).not.toHaveBeenCalled();

    // ② 產生的版本狀態:pager 變 2 / 2,畫面是新答案。
    expect(await screen.findByText("重生後的答案")).toBeInTheDocument();
    await screen.findByText("2 / 2");

    // ③ revisions[1].dbId 已被 fork 回傳的 99 補上 —— 用切走再切回來把它逼出
    // 來(這是唯一從 UI 觀察得到 revision dbId 的方式)。
    fireEvent.click(screen.getByTitle("上一個版本"));
    await screen.findByText("1 / 2");
    await waitFor(() => expect(mocks.setActiveLeaf).toHaveBeenCalledTimes(1));
    expect(mocks.setActiveLeaf).toHaveBeenLastCalledWith(mocks.authRequest, CONV_ID, 2);

    fireEvent.click(screen.getByTitle("下一個版本"));
    await screen.findByText("2 / 2");
    await waitFor(() => expect(mocks.setActiveLeaf).toHaveBeenCalledTimes(2));
    expect(mocks.setActiveLeaf).toHaveBeenLastCalledWith(mocks.authRequest, CONV_ID, 99);
  });
});
