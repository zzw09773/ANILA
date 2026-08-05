// 不變式:「送出去的那一輪,一定帶著正確的對話歷史」。
//
// 這是本包存在的理由。稽核證實:把 app.jsx 的歷史組裝弄壞(讓歷史永遠是
// 空的),本分支基底的既有 413 條測試全綠——因為沒有任何一條把 orchestrator 掛起來、
// 讓它真的送出一個請求。這裡掛的是真的 app.jsx,斷言的是真的送到
// `/v1/chat/completions` 的 body。

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

import {
  mountOrchestrator,
  sendText,
  waitForAnswer,
  waitForIdle,
  historyOf,
  createFakeBackend,
  screen,
  waitFor,
  act,
  fireEvent,
} from "./helpers/orchestrator.jsx";
import { deltaFrame } from "./helpers/fakeBackend.js";

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("orchestrator — 送出與對話歷史", () => {
  it("第一輪:只送使用者這一句", async () => {
    const backend = createFakeBackend();
    backend.enqueueAnswer("第一次回答");
    await mountOrchestrator({ backend });

    await sendText("第一個問題");
    await waitForAnswer("第一次回答");

    expect(backend.chatPayloads).toHaveLength(1);
    expect(historyOf(backend, 0)).toEqual(["user:第一個問題"]);
  });

  it("第三輪:歷史帶著前兩輪的問與答,順序正確", async () => {
    const backend = createFakeBackend();
    backend.enqueueAnswer("回答一").enqueueAnswer("回答二").enqueueAnswer("回答三");
    await mountOrchestrator({ backend });

    await sendText("問題一");
    await waitForAnswer("回答一");
    await waitForIdle();

    await sendText("問題二");
    await waitForAnswer("回答二");
    await waitForIdle();

    await sendText("問題三");
    await waitForAnswer("回答三");
    await waitForIdle();

    expect(backend.chatPayloads).toHaveLength(3);
    // ⚠ 這一條就是「弄壞歷史組裝、既有測試一條都沒紅」的那個缺口。
    expect(historyOf(backend, 2)).toEqual([
      "user:問題一",
      "assistant:回答一",
      "user:問題二",
      "assistant:回答二",
      "user:問題三",
    ]);
  });

  it("串流中的助理訊息不會被折進下一輪的歷史", async () => {
    const backend = createFakeBackend();
    backend.enqueueAnswer("回答一").enqueueAnswer("回答二");
    await mountOrchestrator({ backend });

    await sendText("問題一");
    await waitForAnswer("回答一");
    await waitForIdle();

    await sendText("問題二");
    await waitForAnswer("回答二");

    const history = historyOf(backend, 1);
    // 空字串的 assistant 佔位訊息不該出現在歷史裡。
    expect(history).not.toContain("assistant:");
    expect(history).toEqual(["user:問題一", "assistant:回答一", "user:問題二"]);
  });

  // 逐格推進的串流:驗「串到一半」的畫面,而不是只驗最終狀態。
  // 使用者最常抱怨的是中間狀態(卡住了嗎?停得下來嗎?),
  // 而只驗最終字串的測試對這一段完全沒有意見。
  it("串流進行中:已經串出來的字就要看得到,而且停得下來", async () => {
    const backend = createFakeBackend();
    backend.enqueueManualStream();
    await mountOrchestrator({ backend });

    await sendText("慢慢回答");

    // 第一格
    await act(async () => {
      backend.stream.push(deltaFrame("先講"));
    });
    expect(await screen.findByText("先講")).toBeInTheDocument();
    // 串流中必須是「停止產生」而不是「送出」
    expect(screen.getByLabelText("停止產生")).toBeInTheDocument();

    // 第二格
    await act(async () => {
      backend.stream.push(deltaFrame("第一點"));
    });
    expect(await screen.findByText("先講第一點")).toBeInTheDocument();

    // 使用者按停止 → 串流中止,已經串出來的字保留。
    await act(async () => {
      fireEvent.click(screen.getByLabelText("停止產生"));
    });
    await waitForIdle();
    expect(screen.getByText("先講第一點")).toBeInTheDocument();
  });

  it("助理回答真的被存進後端(不是只留在畫面上)", async () => {
    const backend = createFakeBackend();
    backend.enqueueAnswer("要被存下來的回答");
    await mountOrchestrator({ backend });

    await sendText("請回答");
    await waitForAnswer("要被存下來的回答");
    await waitForIdle();

    const persisted = backend.appendedMessages;
    expect(persisted.map((m) => `${m.role}:${m.content}`)).toEqual([
      "user:請回答",
      "assistant:要被存下來的回答",
    ]);
    // assistant 必須掛在 user 訊息底下 — parent_id 掉了,樹就散了。
    const userRow = persisted[0];
    const assistantRow = persisted[1];
    expect(typeof assistantRow.parent_id).toBe("number");
    expect(userRow.role).toBe("user");
  });
});
