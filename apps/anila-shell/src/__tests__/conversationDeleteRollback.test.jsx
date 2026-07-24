// 樂觀刪除失敗時的還原完整性。
//
// 刪除是樂觀更新:先從畫面拿掉,再打後端。後端失敗時過去只還原
// `conversations`,而 `convMeta`(封存 / 標籤)、`messagesByConv` 與選取狀態
// 都沒還原 —— 對話「復活」後標籤與封存狀態已經永久消失(而且清空後的
// convMeta 已經被 debounce 同步回 ui_settings 了)。
import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const mocks = vi.hoisted(() => ({
  authRequest: vi.fn(async () => ({})),
  listConversations: vi.fn(async () => []),
  getConversation: vi.fn(async () => ({})),
  deleteConversation: vi.fn(async () => ({})),
  putUiSettings: vi.fn(async () => ({ ui_settings: {} })),
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
    deleteConversation: (...args) => mocks.deleteConversation(...args),
    searchConversations: vi.fn(async () => []),
    getUiSettings: vi.fn(async () => ({
      ui_settings: { convMeta: { 7: { tags: ["法務"], archived: false } } },
    })),
    putUiSettings: (...args) => mocks.putUiSettings(...args),
    listActiveBanners: vi.fn(async () => []),
    listAgentFunctions: vi.fn(async () => []),
  };
});

import App from "../app.jsx";
import { ConfirmProvider } from "../confirm.jsx";

if (typeof Element !== "undefined" && !Element.prototype.scrollTo) {
  Element.prototype.scrollTo = function scrollToStub() {};
}

const ROW = {
  id: 7,
  title: "要刪的對話",
  agent_id: null,
  origin: "anila-ui",
  classified: false,
  classification_inherited: false,
  classification_level: "無機密",
  created_at: "2026-07-20T10:00:00Z",
  updated_at: "2026-07-20T10:00:00Z",
};

beforeEach(() => {
  mocks.authRequest.mockReset().mockResolvedValue({});
  mocks.listConversations.mockReset().mockResolvedValue([ROW]);
  mocks.getConversation.mockReset().mockResolvedValue({ ...ROW, messages: [] });
  mocks.deleteConversation.mockReset();
  mocks.putUiSettings.mockReset().mockResolvedValue({ ui_settings: {} });
  vi.stubGlobal("fetch", vi.fn(async () => ({
    ok: true, status: 200, json: async () => ({ data: [] }),
  })));
  window.sessionStorage?.clear?.();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

async function deleteFirstConversation() {
  await screen.findByText("要刪的對話");
  fireEvent.click(screen.getAllByTitle("更多")[0]);
  fireEvent.click(screen.getByText("刪除對話"));
  // 確認對話框
  fireEvent.click(await screen.findByText("刪除"));
}

describe("刪除對話失敗時的還原", () => {
  it("後端失敗 → 對話、標籤都要回來,並讓使用者看到錯誤", async () => {
    mocks.deleteConversation.mockRejectedValue(new Error("後端爆炸"));

    render(<ConfirmProvider><App /></ConfirmProvider>);
    // 標籤先確認有掛上(來自 ui_settings 的 convMeta)。
    expect((await screen.findAllByText("#法務")).length).toBeGreaterThan(0);

    await deleteFirstConversation();

    // 對話回來了。
    await screen.findByText("要刪的對話");
    // ⚠ 這一條是修正重點:過去只還原 conversations,標籤永久消失。
    expect(screen.getAllByText("#法務").length).toBeGreaterThan(0);
    // 錯誤要看得到,不可靜默。
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("後端爆炸");
  });

  it("後端成功 → 對話與它的標籤 meta 都清掉(既有行為不變)", async () => {
    mocks.deleteConversation.mockResolvedValue({});

    render(<ConfirmProvider><App /></ConfirmProvider>);
    expect((await screen.findAllByText("#法務")).length).toBeGreaterThan(0);

    await deleteFirstConversation();

    await waitFor(() => {
      expect(screen.queryByText("要刪的對話")).not.toBeInTheDocument();
    });
    expect(screen.queryAllByText("#法務")).toHaveLength(0);
  });
});
