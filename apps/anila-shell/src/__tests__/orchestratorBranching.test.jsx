// 不變式:編輯重問與重新產生,送出去的內容與存回去的位置都要對。
//
// 這兩條路徑各自有一份幾乎相同的串流 + 存檔程式碼(sendMessage /
// handleEditUser / regenerateMessage),歷史組裝的方式卻不一樣:
//   * 編輯重問 → 歷史切到被編輯那則之前
//   * 重新產生 → 歷史切到對應的使用者訊息之前,而且要走 /branch 開兄弟節點
// 任何一條走錯,使用者看到的是「回答跟我問的不一樣」,而不是任何錯誤訊息。

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

import {
  mountOrchestrator,
  sendText,
  waitForAnswer,
  waitForIdle,
  editUserMessage,
  clickRegenerate,
  historyOf,
  createFakeBackend,
  screen,
  waitFor,
} from "./helpers/orchestrator.jsx";

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("orchestrator — 編輯重問", () => {
  it("送出改寫後的問題,而且歷史裡不含被改掉的舊問題", async () => {
    const backend = createFakeBackend();
    backend
      .enqueueAnswer("第一輪回答")
      .enqueueAnswer("第二輪回答")
      .enqueueAnswer("改寫後的回答");
    await mountOrchestrator({ backend });

    await sendText("原始問題");
    await waitForAnswer("第一輪回答");
    await waitForIdle();

    await sendText("第二個問題");
    await waitForAnswer("第二輪回答");
    await waitForIdle();

    await editUserMessage("改寫後的問題");
    await waitForAnswer("改寫後的回答");
    await waitForIdle();

    expect(backend.chatPayloads).toHaveLength(3);
    // 歷史保留第一輪,第二輪的舊問句被改寫取代 — 不能兩句都在。
    expect(historyOf(backend, 2)).toEqual([
      "user:原始問題",
      "assistant:第一輪回答",
      "user:改寫後的問題",
    ]);
  });

  it("改寫走 /branch 開兄弟節點,不是就地覆寫舊訊息", async () => {
    const backend = createFakeBackend();
    backend.enqueueAnswer("原回答").enqueueAnswer("新回答");
    await mountOrchestrator({ backend });

    await sendText("原問題");
    await waitForAnswer("原回答");
    await waitForIdle();

    await editUserMessage("新問題");
    await waitForAnswer("新回答");
    await waitForIdle();

    const branchCalls = backend.requestsFor("/branch", "POST");
    expect(branchCalls).toHaveLength(1);
    expect(branchCalls[0].body.content).toBe("新問題");
    // 舊問題還在後端(分支保留,不是刪掉重寫)。
    const stored = backend.storedMessages(101).map((m) => m.content);
    expect(stored).toContain("原問題");
    expect(stored).toContain("新問題");
    // 就地覆寫的 PUT 一次都不該有。
    expect(backend.requestsFor("/messages/", "PUT")).toHaveLength(0);
  });

  it("空白內容不送出,也不動後端", async () => {
    const backend = createFakeBackend();
    backend.enqueueAnswer("回答");
    await mountOrchestrator({ backend });

    await sendText("問題");
    await waitForAnswer("回答");
    await waitForIdle();

    const before = backend.chatPayloads.length;
    await editUserMessage("   ");
    expect(backend.chatPayloads).toHaveLength(before);
    expect(backend.requestsFor("/branch", "POST")).toHaveLength(0);
  });
});

describe("orchestrator — 重新產生", () => {
  it("重問同一句,歷史切在該則使用者訊息之前", async () => {
    const backend = createFakeBackend();
    backend
      .enqueueAnswer("第一輪回答")
      .enqueueAnswer("第二輪回答")
      .enqueueAnswer("重試後的回答");
    await mountOrchestrator({ backend });

    await sendText("問題一");
    await waitForAnswer("第一輪回答");
    await waitForIdle();

    await sendText("問題二");
    await waitForAnswer("第二輪回答");
    await waitForIdle();

    await clickRegenerate();
    await waitForAnswer("重試後的回答");
    await waitForIdle();

    expect(historyOf(backend, 2)).toEqual([
      "user:問題一",
      "assistant:第一輪回答",
      "user:問題二",
    ]);
  });

  it("重試的答案存成兄弟節點(/branch),舊答案沒有被刪掉", async () => {
    const backend = createFakeBackend();
    backend.enqueueAnswer("原答案").enqueueAnswer("重試答案");
    await mountOrchestrator({ backend });

    await sendText("問題");
    await waitForAnswer("原答案");
    await waitForIdle();

    await clickRegenerate();
    await waitForAnswer("重試答案");
    await waitForIdle();

    const branchCalls = backend.requestsFor("/branch", "POST");
    expect(branchCalls).toHaveLength(1);
    expect(branchCalls[0].body.role).toBe("assistant");
    expect(branchCalls[0].body.content).toBe("重試答案");

    const stored = backend.storedMessages(101).map((m) => m.content);
    expect(stored).toContain("原答案");
    expect(stored).toContain("重試答案");
  });

  it("重試串流失敗:舊答案要回來,而不是留下一個空白氣泡", async () => {
    const backend = createFakeBackend();
    backend.enqueueAnswer("原本的好答案").enqueueHttpError(503, "模型忙碌中");
    await mountOrchestrator({ backend });

    await sendText("問題");
    await waitForAnswer("原本的好答案");
    await waitForIdle();

    await clickRegenerate();

    // 錯誤要說出來
    await waitFor(() => {
      const alerts = screen.getAllByRole("alert");
      expect(alerts.some((a) => /重試失敗/.test(a.textContent))).toBe(true);
    });
    // 而且原答案必須還在畫面上
    expect(screen.getByText("原本的好答案")).toBeInTheDocument();
  });
});
