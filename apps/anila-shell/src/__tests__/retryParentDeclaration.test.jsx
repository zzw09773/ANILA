// W2-3 缺陷家族收尾:**建立訊息的呼叫端必須宣告父節點**。
//
// 缺陷本體(reviewer 的探針):後端 `append_message()` 在沒收到 `parent_id` 時
// 把新列掛到對話當下的 **active leaf**。訊息還是平面清單時那永遠是對的;長成
// 樹以後就不是了 —— regenerate 串流失敗後 active leaf 還停在**上一則**
// assistant,於是「重試」復原出來的版本會變成那則 assistant 的**子節點**,而
// 不是它的兄弟。層級錯一格之後,下一次 regenerate 從那一列 fork,又從錯的層級
// 再分岔一次。
//
// 這裡用**真的** App(DOM 點擊)+ 一個會複製後端 active-leaf 語意的假伺服器:
// 假伺服器對「沒宣告 parentId」的請求就照後端那樣掛到 active leaf,所以樹長成
// 什麼形狀完全取決於前端有沒有指名父節點。
//
//   F1 regenerate 失敗 → 重試 → 復原的版本與失敗的那個**同父**(兄弟)。
//   F2 承上再 regenerate 一次 → 從正確的層級分岔(仍是同一組兄弟)。
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
import { IMPLICIT_ACTIVE_LEAF } from "../runtime/messageParent.js";

// jsdom 沒有 Element.prototype.scrollTo(訊息區 autoscroll 會呼叫)。
if (typeof Element !== "undefined" && !Element.prototype.scrollTo) {
  Element.prototype.scrollTo = function scrollToStub() {};
}

const CONV_ID = 42;
const TITLE = "重試的父節點";

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

/**
 * 假伺服器 —— 只複製 `conversation_service` 這三件事的語意:
 *   append:`parent_id` 沒給就掛到 active leaf(缺陷的載體);
 *   fork  :掛到來源列的父節點(兄弟);
 *   兩者都把新列設成新的 active leaf。
 *
 * 樹的形狀因此完全由「前端有沒有宣告父節點」決定,這正是要驗的東西。
 */
function makeFakeServer() {
  const rows = [
    { id: 1, role: "user", content: "問題", parent_id: null },
    { id: 2, role: "assistant", content: "答案一", parent_id: 1 },
  ];
  let activeLeaf = 2;
  let nextId = 3;
  const declaredParents = [];

  function parentFor(payload) {
    declaredParents.push(payload.parentId);
    if (typeof payload.parentId === "number") return payload.parentId;
    // `IMPLICIT_ACTIVE_LEAF` 與「完全沒宣告」在線上是同一個請求(parent_id 缺
    // 席),所以假伺服器也一視同仁 —— 缺陷靠這條路成立。
    return activeLeaf;
  }

  return {
    rows,
    declaredParents,
    get activeLeaf() {
      return activeLeaf;
    },
    byId: (id) => rows.find((r) => r.id === id),
    append(payload) {
      const row = {
        id: nextId++,
        role: payload.role,
        content: payload.content,
        parent_id: parentFor(payload) ?? null,
      };
      rows.push(row);
      activeLeaf = row.id;
      return { id: row.id };
    },
    fork(sourceId, patch) {
      const source = rows.find((r) => r.id === sourceId);
      const row = {
        id: nextId++,
        role: "assistant",
        content: patch.content,
        parent_id: source ? source.parent_id : null,
      };
      rows.push(row);
      activeLeaf = row.id;
      return { id: row.id };
    },
  };
}

let server;

function linearDetail() {
  return {
    ...ROW,
    active_leaf_message_id: 2,
    messages: [
      { ...server.byId(1), metadata: {}, created_at: "2026-07-27T10:00:01Z" },
      { ...server.byId(2), metadata: {}, created_at: "2026-07-27T10:00:02Z" },
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
  server = makeFakeServer();
  mocks.authRequest.mockReset().mockResolvedValue({});
  mocks.listConversations.mockReset().mockResolvedValue([ROW]);
  mocks.getConversation.mockReset().mockImplementation(async () => linearDetail());
  mocks.searchConversations.mockReset().mockResolvedValue([]);
  mocks.setActiveLeaf.mockReset().mockResolvedValue({});
  mocks.forkAssistantMessage
    .mockReset()
    .mockImplementation(async (_req, _conv, sourceId, patch) => server.fork(sourceId, patch));
  mocks.appendMessage
    .mockReset()
    .mockImplementation(async (_req, _conv, payload) => server.append(payload));
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

describe("F1:regenerate 失敗 → 重試復原的版本是失敗版本的兄弟,不是子節點", () => {
  it("重試 append 指名父節點 1,新列與訊息 2 同父", async () => {
    mocks.streamChatCompletion.mockRejectedValueOnce(new Error("連線中斷"));

    await openConversation();
    expect(await screen.findByText("答案一")).toBeInTheDocument();
    await regenerate();
    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());

    // 缺陷的前提:regenerate 失敗沒有寫任何東西,伺服器的 active leaf 還停在
    // **上一則 assistant**(2)。隱式 append 就是掛在這裡。
    expect(server.activeLeaf).toBe(2);

    mocks.streamChatCompletion.mockImplementation(async (opts) => {
      opts.onText?.("復原的答案");
      return {};
    });
    fireEvent.click(screen.getByRole("button", { name: /^重試$/ }));
    expect(await screen.findByText("復原的答案")).toBeInTheDocument();

    await waitFor(() => expect(mocks.appendMessage).toHaveBeenCalledTimes(1));
    const recovered = server.byId(3);
    expect(recovered).toBeTruthy();
    expect(recovered.content).toBe("復原的答案");

    // ⚠ 迴歸點:沒宣告父節點時 parent_id 會是 2(active leaf)= 失敗版本的
    // **子節點**。正解是與訊息 2 同父 → 兄弟。
    expect(recovered.parent_id).not.toBe(2);
    expect(recovered.parent_id).toBe(server.byId(2).parent_id);
    expect(recovered.parent_id).toBe(1);

    // 而且是**指名**的,不是碰巧:呼叫端送出的宣告就是 1。
    expect(server.declaredParents).toEqual([1]);
  });
});

describe("F2:重試之後再 regenerate,從正確的層級分岔", () => {
  it("fork 來源是復原的那一列,新兄弟仍掛在 1 底下", async () => {
    mocks.streamChatCompletion.mockRejectedValueOnce(new Error("連線中斷"));

    await openConversation();
    expect(await screen.findByText("答案一")).toBeInTheDocument();
    await regenerate();
    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());

    mocks.streamChatCompletion.mockImplementation(async (opts) => {
      opts.onText?.("復原的答案");
      return {};
    });
    fireEvent.click(screen.getByRole("button", { name: /^重試$/ }));
    expect(await screen.findByText("復原的答案")).toBeInTheDocument();
    await waitFor(() => expect(mocks.appendMessage).toHaveBeenCalledTimes(1));

    mocks.streamChatCompletion.mockImplementation(async (opts) => {
      opts.onText?.("第三個版本");
      return {};
    });
    await regenerate();
    expect(await screen.findByText("第三個版本")).toBeInTheDocument();
    await waitFor(() => expect(mocks.forkAssistantMessage).toHaveBeenCalledTimes(1));

    // 從復原的那一列(3)fork —— 它才是這組兄弟裡最新的持久化成員。
    expect(mocks.forkAssistantMessage.mock.calls[0][2]).toBe(3);

    // ⚠ 迴歸點:若 3 當初被掛成 2 的子節點,這裡 fork 出來的 4 會是 2 的兄弟
    // **的子代**,整組版本從此散在兩個層級。
    const third = server.byId(4);
    expect(third.parent_id).toBe(1);
    const siblings = server.rows.filter(
      (r) => r.role === "assistant" && r.parent_id === 1,
    );
    expect(siblings.map((r) => r.id)).toEqual([2, 3, 4]);
  });
});

describe("宣告契約本體", () => {
  it("IMPLICIT_ACTIVE_LEAF 是必須寫出來的顯式選擇,不是欄位省略", () => {
    // 省略與「我要預設」在線上是同一個請求,所以差別只能存在於呼叫端:
    // 這個 sentinel 不可能從 JSON / 後端回應意外冒出來。
    expect(typeof IMPLICIT_ACTIVE_LEAF).toBe("symbol");
    expect(JSON.parse(JSON.stringify({ parentId: IMPLICIT_ACTIVE_LEAF }))).toEqual({});
  });
});
