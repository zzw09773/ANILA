// 不變式:「後端沒收到 / 沒存到,使用者必須看得見」。
//
// 本專案定義的最壞失敗模式是**靜默成功**:使用者以為發生了什麼,其實沒有。
// 一則沒存進去的回答,重新整理後就消失了;如果當下畫面完全正常,使用者
// 是在幾天後才發現的。所以這裡驗的不是「有沒有呼叫 setRuntimeError」,
// 而是**畫面上真的有那段文字**。

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

import {
  mountOrchestrator,
  sendText,
  waitForAnswer,
  waitForIdle,
  editUserMessage,
  clickRegenerate,
  createFakeBackend,
  screen,
  waitFor,
  act,
  fireEvent,
} from "./helpers/orchestrator.jsx";
import { deltaFrame, errorFrame } from "./helpers/fakeBackend.js";
import { ANSWER_PERSIST_FAILURE_NOTICE } from "../runtime/reservedTurn.js";

// 為什麼斷言的是常數而不是一段正規式:原本寫的是 `/儲存|失敗/`,釘的是**措辭**。
// `wt/shell-reserve` 把文案改得更精確(「這則回答沒有存回對話紀錄，重新整理後就
// 會消失（你的問題已經存好了）。」)之後,那個正規式就對不上了 —— 而產品其實
// 更對了。措辭不是不變式;「使用者拿到的是哪一種失敗說明」才是。
//
// 這一句仍然有牙齒,因為它把兩種失敗分開:回答沒存回去要用這一句,而 head
// 失敗(POST /turn 掛掉)**不能**用它 —— 那時使用者的問題其實已經在資料庫裡,
// 說「沒有存進去」是假的。那一格由 reservedTurn.test.jsx 的
// 「never tells the user a message was not saved when it might have been」守著。

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("orchestrator — 儲存失敗必須可見", () => {
  it("助理回答存檔失敗:氣泡上有標記,而不是安靜地掉了", async () => {
    const backend = createFakeBackend();
    backend.enqueueAnswer("看起來成功的回答");
    // 只讓**寫回預留列**那一次失敗;POST /turn 要成功,否則整輪會在串流前就
    // 中止,驗不到「回答已經在畫面上但沒存進去」。
    // (2026-08-05:助理訊息改由 PUT 寫回預留列,不再是 POST /messages。
    //  注入點跟著搬,斷言完全沒動 —— 要驗的還是同一件事。)
    backend.route("PUT", /\/messages\/\d+$/, (_req, { errorResponse }) =>
      errorResponse(500, "資料庫寫入失敗"),
    );

    await mountOrchestrator({ backend });
    await sendText("請回答");
    await waitForAnswer("看起來成功的回答");

    // 1. 氣泡自己被標記(banner 會被關掉/被忽略,氣泡不會)
    const pin = await screen.findByTestId("message-persist-error");
    expect(pin.textContent).toBe(ANSWER_PERSIST_FAILURE_NOTICE);

    // 2. 全域 banner 也要出現
    await waitFor(() => {
      const alerts = screen.getAllByRole("alert");
      expect(alerts.some((a) => /失敗/.test(a.textContent))).toBe(true);
    });
  });

  it("助理回答存檔回 2xx 但沒有 id:一樣要標記,不能當成功", async () => {
    const backend = createFakeBackend();
    backend.enqueueAnswer("回答內容");
    // 2xx、body 合法、就是沒有 id — 最容易被當成功的那一種。
    backend.route("PUT", /\/messages\/\d+$/, (_req, { jsonResponse }) =>
      jsonResponse({ ok: true }),
    );

    await mountOrchestrator({ backend });
    await sendText("請回答");
    await waitForAnswer("回答內容");

    const pin = await screen.findByTestId("message-persist-error");
    expect(pin.textContent).toBe(ANSWER_PERSIST_FAILURE_NOTICE);
  });

  // 送出 / 編輯重問 / 重新產生仍然是三條各自獨立的存檔程式碼,只是分法在
  // 2026-08-05 之後變了:送出與編輯重問共用 finalizeStreamedAssistant(PUT
  // 寫回預留列),重新產生仍然走 branchMessage + reconcilePersistedAssistant。
  // 只驗送出那一條,另外兩條的「2xx 沒有 id」照樣可以安靜地過去 ——
  // 這個缺口是本包的突變檢查自己抓出來的（persist-2xx-without-id-accepted
  // 一開始存活）。
  it("編輯重問:存檔回 2xx 但沒有 id,一樣要標記", async () => {
    const backend = createFakeBackend();
    backend.enqueueAnswer("原回答").enqueueAnswer("改寫後的回答");
    await mountOrchestrator({ backend });

    await sendText("原問題");
    await waitForAnswer("原回答");
    await waitForIdle();

    // 第一輪正常存檔;從這裡開始寫回預留列回 2xx-without-id。
    backend.route("PUT", /\/messages\/\d+$/, (_req, { jsonResponse }) =>
      jsonResponse({ ok: true }),
    );

    await editUserMessage("改寫後的問題");
    await waitForAnswer("改寫後的回答");

    const pin = await screen.findByTestId("message-persist-error");
    expect(pin.textContent).toBe(ANSWER_PERSIST_FAILURE_NOTICE);
  });

  it("重新產生:分支回 2xx 但沒有 id,一樣要標記", async () => {
    const backend = createFakeBackend();
    backend.enqueueAnswer("原回答").enqueueAnswer("重試回答");
    // 重試的助理訊息走 /branch;第一輪不經過這裡,所以可以直接掛。
    backend.route("POST", /\/branch$/, (req, { jsonResponse }) =>
      req.body?.role === "assistant" ? jsonResponse({ ok: true }) : undefined,
    );

    await mountOrchestrator({ backend });
    await sendText("問題");
    await waitForAnswer("原回答");
    await waitForIdle();

    await clickRegenerate();
    await waitForAnswer("重試回答");

    const pin = await screen.findByTestId("message-persist-error");
    // ⚠ 這一條路徑的文案**不是**上面那一句:重新產生仍然走 branchMessage +
    // reconcilePersistedAssistant,用的是 messageTree.js 的 PERSIST_MISS_NOTICE
    // (它會把失敗原因夾帶進去)。斷言分開寫,才看得出兩條路徑各自有沒有守衛。
    expect(pin.textContent).toContain("沒有存進對話紀錄");
    expect(pin.textContent).not.toBe(ANSWER_PERSIST_FAILURE_NOTICE);
  });

  it("使用者訊息存檔失敗:整輪中止,不會留下一個假的空助理氣泡", async () => {
    const backend = createFakeBackend();
    backend.enqueueAnswer("不該出現的回答");
    // 使用者訊息與預留列現在是同一個交易(POST /turn);它失敗 = 這一輪
    // 連個落腳處都沒有,所以串流一格都不該開始。
    backend.route("POST", /\/turn$/, (_req, { errorResponse }) =>
      errorResponse(500, "使用者訊息寫入失敗"),
    );

    await mountOrchestrator({ backend });
    await sendText("送不出去的問題");

    await waitFor(() => {
      const alerts = screen.getAllByRole("alert");
      expect(alerts.some((a) => /失敗/.test(a.textContent))).toBe(true);
    });
    // 串流根本不該發生
    expect(backend.chatPayloads).toHaveLength(0);
    expect(screen.queryByText("不該出現的回答")).toBeNull();
  });

  it("等待 /turn 時按停止，放行後不呼叫模型", async () => {
    const backend = createFakeBackend();
    backend.disableTitleGeneration();
    backend.deferTurnPersist = true;
    backend.enqueueAnswer("不該出現的回答");
    await mountOrchestrator({ backend });
    await sendText("先問再說");
    await waitFor(() => {
      expect(backend.requestsFor("/turn", "POST").length).toBeGreaterThan(0);
    });
    expect(backend.chatPayloads).toHaveLength(0);
    await act(async () => {
      fireEvent.click(screen.getByLabelText("停止產生"));
    });
    backend.resolveTurnPersist();
    await waitForIdle();
    expect(backend.chatPayloads).toHaveLength(0);
    expect(screen.queryByText("不該出現的回答")).toBeNull();
  });
});

describe("orchestrator — 後端非 2xx 要傳到使用者眼前", () => {
  it("chat completions 回 500:錯誤訊息出現在該則訊息上", async () => {
    const backend = createFakeBackend();
    backend.enqueueHttpError(500, "模型端點暫時無法使用");

    await mountOrchestrator({ backend });
    await sendText("會失敗的問題");

    const err = await screen.findByTestId("message-stream-error");
    expect(err.textContent).toMatch(/模型端點暫時無法使用/);
  });

  it("串流中途 anila.error:已經串出來的字保留,錯誤也照樣顯示", async () => {
    const backend = createFakeBackend();
    backend.enqueueFrames([
      // 先給兩格文字,再宣告失敗 — 使用者已經讀到半截答案了。
      deltaFrame("前半"),
      deltaFrame("段落"),
      errorFrame({ message: "「示範助手」暫時無法使用，請稍後再試。" }),
    ]);

    await mountOrchestrator({ backend });
    await sendText("中途失敗的問題");

    const err = await screen.findByTestId("message-stream-error");
    expect(err.textContent).toMatch(/暫時無法使用/);
    // 半截答案不能被清掉 — 使用者可能要複製它。
    expect(screen.getByText("前半段落")).toBeInTheDocument();
  });

  it("agent 清單載不到:banner 說出來,而不是顯示一個空的助手選單", async () => {
    const backend = createFakeBackend();
    backend.route("GET", "/v1/agents", (_req, { errorResponse }) =>
      errorResponse(503, "agent 服務無法連線"),
    );

    await mountOrchestrator({ backend });

    await waitFor(() => {
      const alerts = screen.getAllByRole("alert");
      expect(alerts.some((a) => /HTTP 503|無法載入/.test(a.textContent))).toBe(true);
    });
  });
});
