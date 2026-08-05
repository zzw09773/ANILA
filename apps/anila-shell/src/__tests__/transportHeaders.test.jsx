// 不變式:「請求離開瀏覽器之前,該掛的東西真的掛上去了」。
//
// 這是本包原本整個缺掉的一格。既有的突變清單全部落在「請求送出之後」
// ——歷史組裝、存檔、分支、錯誤呈現——所以整個 transport 層(CSRF 標頭、
// 對話歸屬標頭、Task 歸屬標頭)沒有任何一條測試看著。實測過的後果:
// 把 `X-CSRF-Token` 從 runtime/api.js 或 runtime/sse.js 刪掉,兩套測試
// 仍然全綠,而瀏覽器裡**每一次送出、編輯、重新產生都會 403**。
//
// 這裡驗的是**掛起來的 orchestrator 真的打出去的那個請求**,不是直接呼叫
// runtime 函式。差別很重要:直接呼叫時 `conversationId` 是測試自己傳的,
// 永遠是對的;而產品真正會壞的那一種是 app.jsx 把**暫態 client id**
// (字串)傳下去 —— `typeof conversationId === "number"` 不成立,標頭
// 靜悄悄地整個不送,沒有任何錯誤訊息。只有掛起來測才看得到。
//
// 分工:`wt/shell-reserve` 的 `csrfHeaders.test.js` 在函式層面釘住
// api.js(buildHeaders / authMultipart)與 sse.js(streamChatCompletion)
// 三條路徑;本檔不重複那三條,改釘 orchestrator 端到端的接線,外加它
// 明說沒收的兩處:`runtime/tasks.js` 與 `sse.js` 的 streamSessionAnswer
// (後者在 transportSessionAnswer.test.js)。

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

import {
  mountOrchestrator,
  sendText,
  waitForAnswer,
  waitForIdle,
  createFakeBackend,
  screen,
  waitFor,
  TEST_CSRF_TOKEN,
  CSRF_COOKIE,
  clearCookie,
} from "./helpers/orchestrator.jsx";
import { headerValue } from "./helpers/fakeBackend.js";

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

/**
 * 對話回合的串流請求。標題產生器打的是**同一個 path**(只是 stream:false),
 * 所以不篩掉的話 `.at(-1)` 拿到的常常是標題那一發 —— 它本來就沒有對話/
 * Task 歸屬標頭,會讓這裡的斷言變成假紅或假綠。
 */
function chatTurns(backend) {
  return backend
    .requestsFor("/v1/chat/completions", "POST")
    .filter((r) => r.body?.stream === true);
}

/** 一輪完整的送出,回傳假後端與那一次串流請求的紀錄。 */
async function sendOneTurn(text = "第一個問題", answer = "回答") {
  const backend = createFakeBackend();
  backend.disableTitleGeneration().enqueueAnswer(answer);
  await mountOrchestrator({ backend });
  await sendText(text);
  await waitForAnswer(answer);
  await waitForIdle();
  return { backend, chatRequest: chatTurns(backend).at(-1) };
}

describe("transport — CSRF 標頭", () => {
  it("串流請求帶著 cookie 裡那個 token(少了它每一次送出都 403)", async () => {
    const { chatRequest } = await sendOneTurn();
    expect(headerValue(chatRequest, "X-CSRF-Token")).toBe(TEST_CSRF_TOKEN);
  });

  it("送出一輪牽涉到的每一個 unsafe 控制面請求都帶著它", async () => {
    const { backend } = await sendOneTurn();
    const unsafe = backend.requests.filter(
      (r) => !["GET", "HEAD", "OPTIONS"].includes(r.method),
    );
    // 至少要有建立對話、建立 Task、落庫兩則訊息;沒有就是這條測試自己失效了。
    expect(unsafe.length).toBeGreaterThanOrEqual(4);
    for (const req of unsafe) {
      expect(
        headerValue(req, "X-CSRF-Token"),
        `${req.method} ${req.path} 沒帶 CSRF 標頭 —— 瀏覽器裡會 403`,
      ).toBe(TEST_CSRF_TOKEN);
    }
  });

  it("token 不見了:使用者看得到失敗,而不是安靜地什麼都沒存到", async () => {
    const backend = createFakeBackend();
    backend.disableTitleGeneration().enqueueAnswer("回答");
    await mountOrchestrator({ backend });
    // 模擬 token 過期/被清掉。真後端此時對每一個 unsafe 請求回 403。
    clearCookie(CSRF_COOKIE);

    await sendText("送不出去的問題");

    await waitFor(() => {
      const alerts = screen.getAllByRole("alert");
      expect(alerts.some((a) => /CSRF|失敗/.test(a.textContent))).toBe(true);
    });
    // 這一條同時是假後端守衛的自我檢查:如果 CSRF 強制其實沒有生效,
    // 上面那個 banner 不會出現,而下面這個對話也會被建出來。
    expect(backend.conversationIds()).toHaveLength(0);
  });
});

describe("transport — 對話歸屬標頭", () => {
  it("X-ANILA-Conversation-Id 帶的是伺服器給的數字 id,不是暫態 client id", async () => {
    const { backend, chatRequest } = await sendOneTurn();
    const [serverConvId] = backend.conversationIds();

    expect(serverConvId).toBeTypeOf("number");
    expect(headerValue(chatRequest, "X-ANILA-Conversation-Id")).toBe(
      String(serverConvId),
    );
    // 它必須是標頭而不是 body 欄位 —— 進了 body 會被原樣轉發到下游
    // OpenAI-compat 端點,那裡不認得這個欄位。
    expect(chatRequest.body.conversation_id).toBeUndefined();
  });

  it("第二輪之後仍然帶著同一個對話 id(不會只有第一輪對)", async () => {
    const backend = createFakeBackend();
    backend
      .disableTitleGeneration()
      .enqueueAnswer("回答一")
      .enqueueAnswer("回答二");
    await mountOrchestrator({ backend });

    await sendText("問題一");
    await waitForAnswer("回答一");
    await waitForIdle();
    await sendText("問題二");
    await waitForAnswer("回答二");
    await waitForIdle();

    const [serverConvId] = backend.conversationIds();
    const chats = chatTurns(backend);
    expect(chats).toHaveLength(2);
    for (const req of chats) {
      expect(headerValue(req, "X-ANILA-Conversation-Id")).toBe(
        String(serverConvId),
      );
    }
  });
});

describe("transport — Task 歸屬標頭", () => {
  it("X-ANILA-Task-Id 帶的是 POST /api/tasks 真的回來的那個 id", async () => {
    const { backend, chatRequest } = await sendOneTurn();
    const taskCreate = backend.requestsFor("/api/tasks", "POST");
    expect(taskCreate).toHaveLength(1);
    // 假後端回 `{ id: 900 }`;標頭要是那個值,不是別的地方湊出來的。
    expect(headerValue(chatRequest, "X-ANILA-Task-Id")).toBe("900");
  });

  it("建立 Task 失敗:聊天照常,但標頭不會硬掰一個值出來", async () => {
    // runtime/tasks.js 的韌性契約是「失敗回 null、聊天不中斷」。
    // 這裡要釘住的是它降級之後**不會**送一個假的 Task 歸屬 ——
    // 送錯的 Task id 會讓用量記到別人的任務上。
    const backend = createFakeBackend();
    backend.disableTitleGeneration().enqueueAnswer("照常的回答");
    backend.route("POST", "/api/tasks", (_req, { errorResponse }) =>
      errorResponse(503, "任務服務暫時無法使用"),
    );

    await mountOrchestrator({ backend });
    await sendText("問題");
    await waitForAnswer("照常的回答");

    const chat = chatTurns(backend).at(-1);
    expect(headerValue(chat, "X-ANILA-Task-Id")).toBeUndefined();
  });
});
