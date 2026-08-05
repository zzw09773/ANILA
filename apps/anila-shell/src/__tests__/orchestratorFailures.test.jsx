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
} from "./helpers/orchestrator.jsx";
import { deltaFrame, errorFrame } from "./helpers/fakeBackend.js";

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
    // 只讓 assistant 那一次 append 失敗;user 那一次要成功,
    // 否則整輪會在串流前就中止,驗不到「回答已經在畫面上但沒存進去」。
    backend.route("POST", /\/messages$/, (req, { errorResponse }) => {
      if (req.body?.role === "assistant") {
        return errorResponse(500, "資料庫寫入失敗");
      }
      return undefined;
    });

    await mountOrchestrator({ backend });
    await sendText("請回答");
    await waitForAnswer("看起來成功的回答");

    // 1. 氣泡自己被標記(banner 會被關掉/被忽略,氣泡不會)
    const pin = await screen.findByTestId("message-persist-error");
    expect(pin.textContent).toMatch(/儲存|失敗/);

    // 2. 全域 banner 也要出現
    await waitFor(() => {
      const alerts = screen.getAllByRole("alert");
      expect(alerts.some((a) => /失敗/.test(a.textContent))).toBe(true);
    });
  });

  it("助理回答存檔回 2xx 但沒有 id:一樣要標記,不能當成功", async () => {
    const backend = createFakeBackend();
    backend.enqueueAnswer("回答內容");
    backend.route("POST", /\/messages$/, (req, { jsonResponse }) => {
      if (req.body?.role === "assistant") {
        // 2xx、body 合法、就是沒有 id — 最容易被當成功的那一種。
        return jsonResponse({ ok: true });
      }
      return undefined;
    });

    await mountOrchestrator({ backend });
    await sendText("請回答");
    await waitForAnswer("回答內容");

    const pin = await screen.findByTestId("message-persist-error");
    expect(pin.textContent).toMatch(/儲存|失敗/);
  });

  // 送出 / 編輯重問 / 重新產生是三條各自獨立的存檔程式碼:
  // 送出走 persistAssistantTurn,另外兩條走 reconcilePersistedAssistant。
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

    // 第一輪正常存檔;從這裡開始 assistant 的 append 回 2xx-without-id。
    backend.route("POST", /\/messages$/, (req, { jsonResponse }) =>
      req.body?.role === "assistant" ? jsonResponse({ ok: true }) : undefined,
    );

    await editUserMessage("改寫後的問題");
    await waitForAnswer("改寫後的回答");

    const pin = await screen.findByTestId("message-persist-error");
    expect(pin.textContent).toMatch(/儲存|失敗/);
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
    expect(pin.textContent).toMatch(/儲存|失敗/);
  });

  it("使用者訊息存檔失敗:整輪中止,不會留下一個假的空助理氣泡", async () => {
    const backend = createFakeBackend();
    backend.enqueueAnswer("不該出現的回答");
    backend.route("POST", /\/messages$/, (req, { errorResponse }) => {
      if (req.body?.role === "user") return errorResponse(500, "使用者訊息寫入失敗");
      return undefined;
    });

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
