import { describe, it, expect, afterEach, vi } from "vitest";
import React from "react";

import {
  mountOrchestrator,
  createFakeBackend,
  sendText,
  waitForAnswer,
  waitForIdle,
  editUserMessage,
  screen,
  waitFor,
  within,
  fireEvent,
  act,
} from "./helpers/orchestrator.jsx";

// 「阻擋」是一個**扼流點**,不是一份呼叫清單。
//
// 這組測試守的不變式:**沒有任何一條路可以把使用者的個資送出去或存下來。**
//
// 為什麼是扼流點而不是清單:這個閘門被驗收找出漏洞兩次。
//   第一次漏掉範本 autosend;修好之後,
//   第二次漏掉編輯重問、引導式重試、建議提示 —— 三條。
// 兩次的徵狀一模一樣:提示列上寫著「將阻擋送出」,而東西照樣送出去、照樣落庫。
// 一份「呼叫端要記得呼叫」的守衛,它的覆蓋率就是一份會漏第三次的清單。
//
// 所以現在閘門在兩個**每條路都必須經過**的地方,而且它們自己從參數裡把要
// 檢查的文字挖出來,不靠呼叫端配合:
//
//   1. `chainTurnHead(convId, content, fn)` —— 新的使用者訊息要落庫只有這一條路,
//      而且 `content` 是必填位置參數:呼叫端連「忘記傳」都做不到。
//   2. `streamWithAbort(convId, opts)` —— 每一次模型呼叫都經過,檢查的文字直接
//      從 `opts.payload` 的最後一則 user 訊息取出。
//
// 新增一條送出路徑的人不必記得呼叫任何東西;他得刻意繞開這兩個函式才躲得掉。
const ID_NUMBER = "A123456789";
const CLEAN = "這是一句沒有個資的話";

// 控制組的素材:頭尾有空白、中間有空行/縮排/tab 的一段貼上內容。
//
// ⚠ 素材本身就是這條測試的一半。原本用的是「這是一句沒有個資的話」——
// 一個前後都沒有空白的字串,於是它**在結構上就不可能**測出送出路徑對空白
// 做了什麼。一條在它要控制的那件事上永遠不會紅的控制組,正是這個工作包
// 在拔掉的那個形狀。
const CLEAN_RAW = "  貼上來的程式碼\n\n    if (x) {\n\t  return 1;\n    }\n\n";

describe("敏感資訊扼流點:每一條路都擋得住", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  const mountBlocked = async (opts = {}) => {
    const backend = createFakeBackend({
      uiSettings: { redactionMode: "block" },
      ...opts,
    });
    await mountOrchestrator({ backend });
    return backend;
  };

  /** 送出一輪乾淨的問答,好讓畫面上有東西可以編輯／重試。 */
  const seedCleanTurn = async (backend) => {
    await sendText(CLEAN);
    // 不比對回答的字面(它會被切成多個 delta frame),只等這一輪真的走完。
    await waitFor(() => {
      expect(backend.chatPayloads.length).toBe(1);
    });
    await waitForIdle();
    return backend.chatPayloads.length;
  };

  it("輸入框送出:擋住,而且沒有落庫", async () => {
    const backend = await mountBlocked();
    await sendText(`我的身分證是 ${ID_NUMBER}`);

    expect(backend.chatPayloads).toHaveLength(0);
    // 落庫也不可以發生 —— 擋在模型那端而讓資料庫存一份原文,等於沒擋。
    expect(backend.appendedMessages).toHaveLength(0);
    expect(JSON.stringify(backend.requests)).not.toContain(ID_NUMBER);
  });

  it("編輯重問:擋住 —— 驗收實測它會送出 2 個 payload 並原文落庫", async () => {
    const backend = await mountBlocked();
    backend.enqueueAnswer("好的");
    const before = await seedCleanTurn(backend);

    await editUserMessage(`我的身分證是 ${ID_NUMBER}`);

    expect(backend.chatPayloads).toHaveLength(before);
    expect(JSON.stringify(backend.chatPayloads)).not.toContain(ID_NUMBER);
    expect(JSON.stringify(backend.appendedMessages)).not.toContain(ID_NUMBER);
  });

  it("引導式重試（自訂調整）:擋住 —— 驗收實測它連 alert 都沒有", async () => {
    const backend = await mountBlocked();
    backend.enqueueAnswer("好的");
    const before = await seedCleanTurn(backend);

    // 開「重新產生」選單 → 自訂調整輸入框 → 送出。
    const triggers = screen.getAllByTitle("重新產生（可選調整方向）");
    await act(async () => {
      fireEvent.click(triggers.at(-1));
    });
    const menu = await screen.findByRole("menu");
    const steerBox = within(menu).getByPlaceholderText("自訂調整…");
    await act(async () => {
      fireEvent.change(steerBox, { target: { value: `照這個身分證 ${ID_NUMBER} 重寫` } });
    });
    await act(async () => {
      fireEvent.keyDown(steerBox, { key: "Enter" });
    });

    expect(backend.chatPayloads).toHaveLength(before);
    expect(JSON.stringify(backend.chatPayloads)).not.toContain(ID_NUMBER);
    // 而且要真的講一句話 —— 驗收實測舊行為是完全靜默。
    expect(screen.getByRole("alert")).toBeTruthy();
  });

  it("後續建議（onPickFollowUp）:擋住 —— 模型可能把個資回敬給你當建議", async () => {
    const backend = await mountBlocked();
    // 建議追問只在信心偏低／中等時才渲染(trust.jsx 的 FollowUpSuggestions)。
    backend.enqueueAnswer("好的", {
      meta: {
        follow_ups: [`要我核對 ${ID_NUMBER} 嗎？`],
        confidence: { level: "low" },
      },
    });
    const before = await seedCleanTurn(backend);

    const suggestion = await screen.findByText(`要我核對 ${ID_NUMBER} 嗎？`);
    await act(async () => {
      fireEvent.click(suggestion.closest("button"));
    });

    expect(backend.chatPayloads).toHaveLength(before);
    expect(JSON.stringify(backend.chatPayloads)).not.toContain(ID_NUMBER);
    expect(JSON.stringify(backend.appendedMessages)).not.toContain(ID_NUMBER);
  });

  // 對話標題是**最早**離開瀏覽器的東西:它在使用者訊息落庫之前就被拿去
  // 開對話、開 Task,而且還會被送去給模型摘要。只擋後面兩個扼流點的話,
  // 訊息本身保住了,標題卻已經帶著身分證號出去了。
  //
  // ⚠ 這一條是**第二道**保險,不是那一行的釘子:走 composer 的話,`chat.jsx`
  // 的閘門先擋,所以只把 app.jsx 那一行拿掉它照樣綠。真正釘住那一行的是
  // 下面那條(`Task 標題`)—— 兩條都留著,一條證明結果、一條證明成因。
  it("對話標題:擋住 —— 連開對話那一步都不可以發生", async () => {
    const backend = await mountBlocked();
    await sendText(`我的身分證是 ${ID_NUMBER}`);

    const created = backend.requests.filter(
      (r) => r.path === "/api/conversations" && r.method === "POST",
    );
    expect(created).toHaveLength(0);
    // 標題摘要走的是 `stream:false` 的模型呼叫,不記進 chatPayloads —— 所以
    // 直接看所有出去過的請求。
    expect(JSON.stringify(backend.requests)).not.toContain(ID_NUMBER);
  });

  // ── 送出路徑最前面那道閘門(app.jsx `sendMessage` 開頭)的獨立釘子 ──────
  //
  // 那一行守的東西,兩個扼流點**都來不及**守:`ensureConversation` 與
  // `createTaskForConversation` 都在它們之前跑,而且都拿這段草稿當標題送上去。
  //
  // 要單獨釘住它,得找一條**不經過 composer 閘門**的送出路徑 —— 建議追問就是:
  // `app.jsx` 把它接成 `onPickFollowUp={(q) => sendMessage(q, [], {})}`,直接進
  // sendMessage。再讓第一輪的 Task 建立失敗(它的失敗契約是靜默回 null),
  // 於是 taskId 還是空的,下一輪會再試一次建立 —— 那次建立就會把整句話
  // 當標題送出去。
  //
  // 所以:拿掉 app.jsx 那一行 → POST /api/tasks 帶著身分證號 → 這條紅。
  // 兩個扼流點原封不動,chatPayloads 與 appendedMessages 依然乾淨。
  it("Task 標題:只有最前面那道閘門擋得住 —— 兩個扼流點都來不及", async () => {
    const backend = createFakeBackend({ uiSettings: { redactionMode: "block" } });
    // Task 建立一律失敗 → taskId 永遠是 null → 每一輪都會再試一次建立。
    backend.route("POST", "/api/tasks", (req, { errorResponse }) =>
      errorResponse(503, "task 服務暫停"),
    );
    await mountOrchestrator({ backend });
    backend.enqueueAnswer("好的", {
      meta: {
        follow_ups: [`要我核對 ${ID_NUMBER} 嗎？`],
        confidence: { level: "low" },
      },
    });
    await seedCleanTurn(backend);

    const suggestion = await screen.findByText(`要我核對 ${ID_NUMBER} 嗎？`);
    await act(async () => {
      fireEvent.click(suggestion.closest("button"));
    });

    // Task 標題 = 那句建議的全文。它在兩個扼流點之前就送出去了。
    const taskPosts = backend.requestsFor("/api/tasks", "POST");
    expect(JSON.stringify(taskPosts)).not.toContain(ID_NUMBER);
    // 對照:扼流點自己仍然是乾淨的 —— 這條紅的時候,紅的原因只會是那一行。
    expect(JSON.stringify(backend.chatPayloads)).not.toContain(ID_NUMBER);
    expect(JSON.stringify(backend.appendedMessages)).not.toContain(ID_NUMBER);
  });

  it("對照組:同樣這幾條路,warn 的時候要照常運作", async () => {
    const backend = createFakeBackend({ uiSettings: { redactionMode: "warn" } });
    await mountOrchestrator({ backend });
    backend.enqueueAnswer("好的");

    await sendText(`我的身分證是 ${ID_NUMBER}`);

    // warn 不擋送出(它只說一句話)—— 這一條綠,上面那幾條才有意義。
    await waitFor(() => {
      expect(backend.chatPayloads.length).toBeGreaterThan(0);
    });
    expect(JSON.stringify(backend.chatPayloads)).toContain(ID_NUMBER);
  });

  // ⚠ 對照組的對照組:**沒有**被偵測到東西的草稿,兩個扼流點都不可以動它。
  //
  // 這一條釘的是「平台不會替你改寫你打的字」。曾經有一個模式會在畫面上把
  // 偵測到的片段換成星號;那個模式沒了,而這個不變式必須留下來。
  //
  // ⚠ 但要釘得**準**:送出路徑真的會動一個地方 —— `chat.jsx` 的 submit 會
  // `text.trim()`,所以頭尾空白會被去掉。所以不變式不是「一個位元組都不差」
  // (那句話是假的,設定頁曾經這樣寫),而是:
  //
  //   **被拿掉的東西只能是頭尾空白,內容一個字都不准變。**
  //
  // 底下最後兩行就是這句話的機械化版本:送出去的那一串必須是原草稿的子字串,
  // 而且扣掉它之後剩下的只能是空白。任何一種「改寫內容」都過不了這兩行。
  it("控制組:沒有偵測到東西的草稿,模型端與落庫端都是原封不動的原文", async () => {
    const backend = createFakeBackend({ uiSettings: { redactionMode: "block" } });
    await mountOrchestrator({ backend });
    backend.enqueueAnswer("好的");

    await sendText(CLEAN_RAW);
    await waitFor(() => {
      expect(backend.chatPayloads.length).toBe(1);
    });

    const expected = CLEAN_RAW.trim();

    // 模型呼叫路徑:最後一則 user 訊息的內容。
    const sentToModel = backend.chatPayloads[0].messages.at(-1).content;
    expect(sentToModel).toBe(expected);

    // 落庫路徑:POST /turn 寫進去的那一列。兩條路必須拿到**同一串**。
    const persistedUser = backend.appendedMessages.find((m) => m.role === "user");
    expect(persistedUser).toBeTruthy();
    expect(persistedUser.content).toBe(expected);

    // 中間的空行、縮排與 tab 原封不動 —— 貼程式碼的人靠這個。
    expect(sentToModel).toContain("\n\n    if (x) {");
    expect(sentToModel).toContain("\t  return 1;");

    // 不變式本體:送出去的是原草稿的子字串,而扣掉它之後剩下的只有空白。
    expect(CLEAN_RAW).toContain(sentToModel);
    expect(CLEAN_RAW.replace(sentToModel, "")).toMatch(/^\s*$/);
  });
});
