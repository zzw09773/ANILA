// W2-3 缺陷家族的兩個潛伏成員 —— 「使用者訊息沒存成,但 assistant 照存」。
//
// `sendMessage` 與 `handleEditUser` 都會先落地一則 user 訊息、再落地 assistant。
// 兩條路徑本來都**吞掉** user 那一步的失敗然後繼續寫 assistant;而 assistant 的
// append 沒有宣告父節點,於是它會被掛到伺服器當下的 active leaf —— 也就是「上
// 一輪的某個節點」,一個跟這則回應毫無關係的位置。
//
// 決策(測試名字裡講明):**不新增無主的 assistant**。掛錯位置的資料看起來成
// 功、重整後才發現對話結構壞掉,而且沒有任何後續動作能自動修正它(rating /
// regenerate 會一路沿著錯的位置長下去)。寧可把失敗攤在使用者面前讓他重跑。
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
  editUserMessage: vi.fn(async () => ({})),
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
    editUserMessage: (...args) => mocks.editUserMessage(...args),
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
import { ORPHAN_ASSISTANT_MESSAGE } from "../runtime/messageParent.js";

if (typeof Element !== "undefined" && !Element.prototype.scrollTo) {
  Element.prototype.scrollTo = function scrollToStub() {};
}

const CONV_ID = 42;
const TITLE = "無主的回應";

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

const EMPTY_DETAIL = { ...ROW, active_leaf_message_id: null, messages: [] };

/** 已有一輪問答,而且 active leaf 就停在那則 assistant 上(隱式 append 的落點)。 */
const LINEAR_DETAIL = {
  ...ROW,
  active_leaf_message_id: 2,
  messages: [msg(1, "user", "原始問題", null, 1), msg(2, "assistant", "答案一", 1, 2)],
};

async function openConversation() {
  render(<ConfirmProvider><App /></ConfirmProvider>);
  const rows = await screen.findAllByText(TITLE);
  fireEvent.click(rows[0]);
}

async function sendFromComposer(text) {
  const box = await screen.findByPlaceholderText(/問 ANILA 任何事情/);
  fireEvent.change(box, { target: { value: text } });
  fireEvent.click(screen.getByLabelText("送出"));
}

function assistantAppends() {
  return mocks.appendMessage.mock.calls.filter((c) => c[2]?.role === "assistant");
}

beforeEach(() => {
  mocks.authRequest.mockReset().mockResolvedValue({});
  mocks.listConversations.mockReset().mockResolvedValue([ROW]);
  mocks.getConversation.mockReset().mockResolvedValue(LINEAR_DETAIL);
  mocks.searchConversations.mockReset().mockResolvedValue([]);
  mocks.setActiveLeaf.mockReset().mockResolvedValue({});
  mocks.forkAssistantMessage.mockReset().mockResolvedValue({});
  mocks.appendMessage.mockReset().mockResolvedValue({ id: 99 });
  mocks.editUserMessage.mockReset().mockResolvedValue({ id: 88 });
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

describe("F3a sendMessage:user 訊息落地失敗 → 決策=不新增無主的 assistant", () => {
  it("assistant 完全不 append(不掛到 active leaf),改用錯誤橫幅告知這一輪沒存到", async () => {
    mocks.getConversation.mockResolvedValue(EMPTY_DETAIL);
    mocks.appendMessage.mockImplementation(async (_req, _conv, payload) => {
      if (payload?.role === "user") throw new Error("使用者訊息落地失敗");
      return { id: 99 };
    });
    mocks.streamChatCompletion.mockImplementation(async (opts) => {
      opts.onText?.("這段回答生得出來,但沒有位置可以放");
      return {};
    });

    await openConversation();
    await sendFromComposer("新的問題");

    // 串流結果還在畫面上 —— 決策不是「丟掉使用者看到的東西」。
    expect(
      await screen.findByText("這段回答生得出來,但沒有位置可以放"),
    ).toBeInTheDocument();

    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(/不會寫入資料庫/),
    );
    expect(screen.getByRole("alert")).toHaveTextContent(ORPHAN_ASSISTANT_MESSAGE);

    // ⚠ 迴歸點:原行為會在這裡 append 一則沒有 parent 的 assistant,伺服器把它
    // 掛到 active leaf —— 一個跟這一輪無關的位置。
    expect(assistantAppends()).toHaveLength(0);
  });
});

describe("F3b handleEditUser:編輯後的 user 兄弟節點落地失敗 → 決策=不新增無主的 assistant", () => {
  it("assistant 完全不 append(不掛到上一則回覆底下),改用錯誤橫幅告知", async () => {
    mocks.editUserMessage.mockRejectedValue(new Error("訊息編輯儲存失敗"));
    mocks.streamChatCompletion.mockImplementation(async (opts) => {
      opts.onText?.("針對改過的問題重新回答");
      return {};
    });

    await openConversation();
    expect(await screen.findByText("答案一")).toBeInTheDocument();

    fireEvent.click(screen.getByTitle("編輯"));
    const box = await screen.findByDisplayValue("原始問題");
    fireEvent.change(box, { target: { value: "改過的問題" } });
    fireEvent.keyDown(box, { key: "Enter", ctrlKey: true });

    expect(await screen.findByText("針對改過的問題重新回答")).toBeInTheDocument();

    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(/不會寫入資料庫/),
    );
    expect(screen.getByRole("alert")).toHaveTextContent(ORPHAN_ASSISTANT_MESSAGE);

    // ⚠ 迴歸點:編輯失敗時 active leaf 還停在訊息 2(上一則回覆),隱式 append
    // 會讓新回應變成它的子節點 —— 編輯本來要長的是**兄弟**分支。
    expect(assistantAppends()).toHaveLength(0);
  });

  it("編輯成功時 assistant 指名剛長出來的 user 兄弟節點當父節點", async () => {
    mocks.editUserMessage.mockResolvedValue({ id: 88 });
    mocks.streamChatCompletion.mockImplementation(async (opts) => {
      opts.onText?.("針對改過的問題重新回答");
      return {};
    });

    await openConversation();
    expect(await screen.findByText("答案一")).toBeInTheDocument();

    fireEvent.click(screen.getByTitle("編輯"));
    const box = await screen.findByDisplayValue("原始問題");
    fireEvent.change(box, { target: { value: "改過的問題" } });
    fireEvent.keyDown(box, { key: "Enter", ctrlKey: true });

    await waitFor(() => expect(assistantAppends()).toHaveLength(1));
    // 不是 active leaf(2),而是編輯長出來的兄弟(88)。
    expect(assistantAppends()[0][2].parentId).toBe(88);
  });
});
