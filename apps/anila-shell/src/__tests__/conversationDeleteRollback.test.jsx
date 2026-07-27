// 樂觀刪除失敗時的還原完整性。
//
// 刪除是樂觀更新:先從畫面拿掉,再打後端。後端失敗時過去只還原
// `conversations`,而 `convMeta`(封存 / 標籤)、`messagesByConv` 與選取狀態
// 都沒還原 —— 對話「復活」後標籤與封存狀態已經永久消失(而且清空後的
// convMeta 已經被 debounce 同步回 ui_settings 了)。
import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, act, waitFor } from "@testing-library/react";

const mocks = vi.hoisted(() => ({
  authRequest: vi.fn(async () => ({})),
  listConversations: vi.fn(async () => []),
  getConversation: vi.fn(async () => ({})),
  deleteConversation: vi.fn(async () => ({})),
  searchConversations: vi.fn(async () => []),
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
    searchConversations: (...args) => mocks.searchConversations(...args),
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

// 刪除請求還在飛的期間,使用者從命令面板開起來的「另一則」對話。
// 它刻意**不在**本地清單裡 —— 這樣才代表「請求期間才出現的新列」。
const LATER_ROW = {
  id: 8,
  title: "另一則對話",
  agent_id: null,
  origin: "anila-ui",
  classified: false,
  classification_inherited: false,
  classification_level: "無機密",
  created_at: "2026-07-21T10:00:00Z",
  updated_at: "2026-07-21T10:00:00Z",
};
const LATER_BODY = "另一則對話的內容";

beforeEach(() => {
  mocks.authRequest.mockReset().mockResolvedValue({});
  mocks.listConversations.mockReset().mockResolvedValue([ROW]);
  mocks.getConversation.mockReset().mockImplementation(async (_req, id) =>
    id === LATER_ROW.id
      ? {
          ...LATER_ROW,
          messages: [
            {
              id: 11,
              role: "assistant",
              content: LATER_BODY,
              metadata: {},
              created_at: "2026-07-21T10:00:00Z",
            },
          ],
        }
      : { ...ROW, messages: [] },
  );
  mocks.searchConversations.mockReset().mockResolvedValue([]);
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
  // 選取後標題會同時出現在側欄與標題列 → 一律用 findAllByText。
  await screen.findAllByText("要刪的對話");
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

  // 樂觀刪除的 rollback 過去是「整份舊 conversations 快照蓋回去 + 依舊的
  // wasSelected 強制選回」。刪除請求在飛的那段時間使用者完全可以再開一則
  // 對話 —— 那一還原就把新列連同它的訊息一起抹掉,並把選取搶回被刪的那則。
  it("rollback 只補回被刪的那一列:請求期間開的新對話不被抹掉、選取也不被搶回", async () => {
    let rejectDelete;
    mocks.deleteConversation.mockImplementation(
      () => new Promise((_resolve, reject) => {
        rejectDelete = () => reject(new Error("後端爆炸"));
      }),
    );
    mocks.searchConversations.mockResolvedValue([LATER_ROW]);

    render(<ConfirmProvider><App /></ConfirmProvider>);

    // 先選取要刪的那則(這樣 wasSelected=true,才會走搶回選取的路徑)。
    fireEvent.click(await screen.findByText("要刪的對話"));
    await waitFor(() => {
      expect(mocks.getConversation).toHaveBeenCalledWith(expect.anything(), ROW.id, { tree: true });
    });

    await deleteFirstConversation();
    await waitFor(() => { expect(mocks.deleteConversation).toHaveBeenCalled(); });

    // ── 刪除請求還掛著,使用者開了另一則對話 ──────────────────────────
    fireEvent.click(screen.getByText("搜尋 / 跳轉"));
    const input = await screen.findByLabelText("搜尋對話或跳轉動作");
    fireEvent.change(input, { target: { value: "另一則" } });
    await screen.findByText("另一則對話", {}, { timeout: 2000 });
    fireEvent.keyDown(input, { key: "Enter" });
    // 真的開起來了:內容渲染出來 = selectedConvId 已經是 8。
    await screen.findByText(LATER_BODY);

    // ── 這時後端才回失敗 ────────────────────────────────────────────
    await act(async () => {
      rejectDelete();
      await Promise.resolve();
    });

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("後端爆炸");

    // ⚠ 修正重點 ①:請求期間開的新列不可以被整份快照抹掉。
    expect(screen.getAllByText("另一則對話").length).toBeGreaterThan(0);
    // ⚠ 修正重點 ②:選取不可以被搶回被刪的那則 —— 內容還是「另一則」的。
    expect(screen.getByText(LATER_BODY)).toBeInTheDocument();
    // 被刪的那一列本身仍然要補回來(原本的還原保證不可退化)。
    expect(screen.getAllByText("要刪的對話").length).toBeGreaterThan(0);
    expect(screen.getAllByText("#法務").length).toBeGreaterThan(0);
  });

  it("使用者沒有改變選取時,rollback 仍然把選取還給被刪的那則", async () => {
    mocks.deleteConversation.mockRejectedValue(new Error("後端爆炸"));

    render(<ConfirmProvider><App /></ConfirmProvider>);
    fireEvent.click(await screen.findByText("要刪的對話"));
    // 選取生效後標題列也會出現同一個標題 → 側欄 + 標題列共兩處。
    await waitFor(() => {
      expect(screen.getAllByText("要刪的對話").length).toBeGreaterThan(1);
    });

    await deleteFirstConversation();

    await screen.findByRole("alert");
    // 選取還原了 → 標題列又出現這則對話的標題(只剩側欄那一份就是沒還原)。
    await waitFor(() => {
      expect(screen.getAllByText("要刪的對話").length).toBeGreaterThan(1);
    });
  });
});
