// 涉密安全(整合層):從命令面板開啟一則「本地清單沒有」的對話時,
// 訊息內容在拿到完整 conversation(含 classified / classification_level)
// 之前**一個字都不能出現**;拿到之後必須帶著鑑識浮水印一起渲染。
//
// 這條之所以要在 App 這一層測:退化是「渲染時 selectedConv=null →
// isClassified=false」造成的,拆開任何一個元件單測都看不出來。
import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, act, waitFor } from "@testing-library/react";

const mocks = vi.hoisted(() => ({
  authRequest: vi.fn(async () => ({})),
  listConversations: vi.fn(async () => []),
  getConversation: vi.fn(async () => ({})),
  searchConversations: vi.fn(async () => []),
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
    getUiSettings: vi.fn(async () => ({ ui_settings: {} })),
    putUiSettings: vi.fn(async () => ({ ui_settings: {} })),
    listActiveBanners: vi.fn(async () => []),
    listAgentFunctions: vi.fn(async () => []),
  };
});

import App from "../app.jsx";
import { ConfirmProvider } from "../confirm.jsx";

const SECRET_BODY = "飛彈射程參數：不可外洩";

function classifiedDetail() {
  return {
    id: 42,
    title: "機密案",
    agent_id: null,
    origin: "anila-ui",
    classified: true,
    classified_at: "2026-07-24T10:00:00Z",
    classification_inherited: false,
    classification_level: "極機密",
    created_at: "2026-07-24T09:00:00Z",
    updated_at: "2026-07-24T10:00:00Z",
    messages: [
      { id: 1, role: "assistant", content: SECRET_BODY, metadata: {}, created_at: "2026-07-24T10:00:00Z" },
    ],
  };
}

function searchHit(over = {}) {
  return {
    id: 42,
    title: "機密案",
    agent_id: null,
    origin: "anila-ui",
    classified: true,
    classification_inherited: false,
    classification_level: "極機密",
    created_at: "2026-07-24T09:00:00Z",
    updated_at: "2026-07-24T10:00:00Z",
    snippet: null,
    ...over,
  };
}

function renderApp() {
  return render(
    <ConfirmProvider>
      <App />
    </ConfirmProvider>,
  );
}

async function openPaletteAndSearch(query) {
  fireEvent.click(await screen.findByText("搜尋 / 跳轉"));
  const input = await screen.findByLabelText("搜尋對話或跳轉動作");
  fireEvent.change(input, { target: { value: query } });
  return input;
}

// jsdom 沒有實作 Element.prototype.scrollTo(訊息區的 autoscroll 會呼叫)。
// 純環境補墊,與受測邏輯無關。
if (typeof Element !== "undefined" && !Element.prototype.scrollTo) {
  Element.prototype.scrollTo = function scrollToStub() {};
}

beforeEach(() => {
  mocks.authRequest.mockReset().mockResolvedValue({});
  mocks.listConversations.mockReset().mockResolvedValue([]); // 本地清單刻意留空
  mocks.getConversation.mockReset().mockResolvedValue(classifiedDetail());
  mocks.searchConversations.mockReset().mockResolvedValue([]);
  // /v1/agents 走裸 fetch。回一個空清單就好 —— agent 不是本測試的關注點,
  // 但**不能讓它失敗**,否則 runtimeError 會被 agent 錯誤佔走,干擾斷言。
  vi.stubGlobal("fetch", vi.fn(async () => ({
    ok: true,
    status: 200,
    json: async () => ({ data: [] }),
  })));
  window.sessionStorage?.clear?.();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("從命令面板開啟本地清單沒有的對話", () => {
  it("**核心斷言**:只有 ID、無本地 conversation 時,classified 對話不會以未分類狀態渲染", async () => {
    mocks.searchConversations.mockResolvedValue([searchHit()]);
    // 詳細資料刻意「掛住」,重現「已經選了 id、conversation 還沒到手」那一刻。
    let releaseDetail;
    mocks.getConversation.mockImplementation(
      () => new Promise((resolve) => { releaseDetail = () => resolve(classifiedDetail()); }),
    );

    renderApp();
    const input = await openPaletteAndSearch("機密");

    // 伺服器搜尋(debounce 260ms)後,面板列出這則對話。
    await screen.findByText("機密案", {}, { timeout: 2000 });

    fireEvent.keyDown(input, { key: "Enter" });

    // ── hydrate 尚未完成 ──────────────────────────────────────────────
    await waitFor(() => {
      expect(mocks.getConversation).toHaveBeenCalledWith(expect.anything(), 42, { tree: true });
    });
    // 訊息內文一個字都不能出現。
    expect(screen.queryByText(SECRET_BODY)).not.toBeInTheDocument();
    expect(document.body.textContent).not.toContain(SECRET_BODY);

    // ── hydrate 完成 ─────────────────────────────────────────────────
    await act(async () => { releaseDetail(); });

    await screen.findByText(SECRET_BODY);
    // 訊息出現的同一刻,鑑識浮水印(密等 · 使用者 · trace)必須也在 ——
    // 這一條就是 REJECT 的原點:過去只存 id,渲染時 isClassified=false,
    // 訊息照樣畫出來但整個機密外觀與限制都消失了。
    await waitFor(() => {
      expect(screen.getByText(/極機密 · tester@ncsist\.org\.tw/)).toBeInTheDocument();
    });
    // 分類徽章同步出現 → conversation 物件確實已 hydrate。
    expect(screen.getAllByTitle(/此對話分類等級：極機密/).length).toBeGreaterThan(0);
  });

  it("hydrate 期間給使用者看得到的「開啟對話中…」,而不是畫面沒反應", async () => {
    mocks.searchConversations.mockResolvedValue([searchHit()]);
    let releaseDetail;
    mocks.getConversation.mockImplementation(
      () => new Promise((resolve) => { releaseDetail = () => resolve(classifiedDetail()); }),
    );

    renderApp();
    const input = await openPaletteAndSearch("機密");
    await screen.findByText("機密案", {}, { timeout: 2000 });
    fireEvent.keyDown(input, { key: "Enter" });

    await screen.findByText("開啟對話中…");
    await act(async () => { releaseDetail(); });
    await waitFor(() => {
      expect(screen.queryByText("開啟對話中…")).not.toBeInTheDocument();
    });
  });

  it("跨 origin(ANILALM)的搜尋結果不會出現在面板裡", async () => {
    mocks.searchConversations.mockResolvedValue([
      searchHit({ id: 99, title: "知識庫的機密對話", origin: "anilalm" }),
      searchHit({ id: 42, title: "本 app 的對話", origin: "anila-ui", classified: false }),
    ]);

    renderApp();
    // 面板對伺服器結果也會再套一次文字過濾 → 查詢字串要同時命中兩列標題,
    // 這樣「知識庫那列不見了」才是 origin 過濾的功勞,不是文字沒對上。
    await openPaletteAndSearch("對話");

    await screen.findByText("本 app 的對話", {}, { timeout: 2000 });
    expect(screen.queryByText("知識庫的機密對話")).not.toBeInTheDocument();
  });

  it("伺服器結果的 updated_at 有轉成面板讀的 updatedAt(不會全部顯示「剛剛」)", async () => {
    mocks.searchConversations.mockResolvedValue([
      searchHit({
        id: 51,
        title: "很久以前的對話",
        classified: false,
        created_at: "2020-01-01T00:00:00Z",
        updated_at: "2020-01-01T00:00:00Z",
      }),
    ]);

    renderApp();
    await openPaletteAndSearch("很久");
    await screen.findByText("很久以前的對話", {}, { timeout: 2000 });
    // snake_case 沒轉成 camelCase 時 relativeLabel(undefined) 一律回「剛剛」,
    // 排序也會壞掉(全部被當成最新)。
    expect(screen.queryByText("剛剛")).not.toBeInTheDocument();
    expect(screen.getByText(/年前$/)).toBeInTheDocument();
  });

  // ── 渲染閘門的逃生口 ────────────────────────────────────────────────
  // 閘門規則是「conversation 物件沒到手就一則訊息都不渲染」。代價是只要
  // selectedConvId 變成孤兒(清單裡找不到),lazy hydrate effect 會直接
  // return,畫面就永遠停在「對話載入中…」—— 不重試、也沒有退出。
  it("較晚抵達的初始清單移走了已 hydrate 的對話 → 自動重新 hydrate,不會永久卡在「對話載入中…」", async () => {
    mocks.searchConversations.mockResolvedValue([searchHit()]);
    // 初始清單刻意「掛住」,而且回來時**不含** 42(清單有分頁/筆數上限時
    // 完全可能發生)。使用者在它回來之前就先用搜尋開了 42。
    let releaseList;
    mocks.listConversations.mockImplementation(
      () => new Promise((resolve) => { releaseList = () => resolve([]); }),
    );

    renderApp();
    const input = await openPaletteAndSearch("機密");
    await screen.findByText("機密案", {}, { timeout: 2000 });
    fireEvent.keyDown(input, { key: "Enter" });

    // 先確認確實 hydrate 成功、內容看得到。
    await screen.findByText(SECRET_BODY);

    // 這時初始清單才回來,把 42 從清單裡刷掉,selectedConvId 卻還留著。
    await act(async () => { releaseList(); });

    // ⚠ 核心斷言:使用者必須有一條出路,不能永遠對著「對話載入中…」乾等。
    await waitFor(
      () => { expect(screen.queryByText("對話載入中…")).not.toBeInTheDocument(); },
      { timeout: 3000 },
    );
    // 而且出路要「真的可用」——訊息與鑑識浮水印一起回來,不是降級成
    // 未分類姿態,也不是留下一個空殼。
    await screen.findByText(SECRET_BODY);
    expect(screen.getByText(/極機密 · tester@ncsist\.org\.tw/)).toBeInTheDocument();
  });

  it("孤兒對話後端也抓不到 → 清除選取並說明原因,不留在載入中", async () => {
    mocks.searchConversations.mockResolvedValue([searchHit()]);
    let releaseList;
    mocks.listConversations.mockImplementation(
      () => new Promise((resolve) => { releaseList = () => resolve([]); }),
    );

    renderApp();
    const input = await openPaletteAndSearch("機密");
    await screen.findByText("機密案", {}, { timeout: 2000 });
    fireEvent.keyDown(input, { key: "Enter" });
    await screen.findByText(SECRET_BODY);

    // 自癒時後端已經沒有這則對話(例如別的分頁把它刪了)。
    mocks.getConversation.mockRejectedValue(new Error("對話不存在"));
    await act(async () => { releaseList(); });

    const alert = await screen.findByRole("alert", {}, { timeout: 3000 });
    expect(alert.textContent).toContain("對話不存在");
    await waitFor(() => {
      expect(screen.queryByText("對話載入中…")).not.toBeInTheDocument();
    });
    // 選取被放開 → 回到「新對話」的空狀態,機密內容當然也不留。
    expect(screen.queryByText(SECRET_BODY)).not.toBeInTheDocument();
  });

  it("後端說這則對話屬於別的 app → 拒絕開啟並明說原因(不靜默失敗)", async () => {
    // 搜尋結果沒帶 origin(舊 payload),第二道防線在 hydrate 時才發現。
    mocks.searchConversations.mockResolvedValue([searchHit({ origin: undefined })]);
    mocks.getConversation.mockResolvedValue({ ...classifiedDetail(), origin: "anilalm" });

    renderApp();
    const input = await openPaletteAndSearch("機密");
    await screen.findByText("機密案", {}, { timeout: 2000 });
    fireEvent.keyDown(input, { key: "Enter" });

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("ANILALM");
    expect(screen.queryByText(SECRET_BODY)).not.toBeInTheDocument();
    expect(document.body.textContent).not.toContain(SECRET_BODY);
  });
});
