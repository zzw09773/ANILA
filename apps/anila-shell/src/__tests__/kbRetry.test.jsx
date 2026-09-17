// 「改用院內規章重查」——使用者事後自救的那一顆。
//
// 設計 §8 的洞是結構性的:Router 判錯(該搜規章而沒搜)時,畫面上**什麼標記
// 都不會有**——使用者拿到一個沒有依據的規定答案,而且沒有任何提示。擁有者
// 08-07 裁定做「事後版」:答案下方一顆按鈕,按下去用**同一個問句**重跑一次、
// 強制查規章。事前的開關(輸入框旁邊「查規章」)被否決,理由是使用者問之前
// 哪知道這題該查規章——他是看到答案才知道的。
//
// 這一包要消滅的形狀有三個,每一個都在本檔有對應的測試:
//
// 1. **靜默失效**:選項畫得出來、點得下去、什麼也沒送到後端。既有的四個選項
//    唯一的後端通道是把 steer 文字串進 user 訊息(app.jsx:2246-2248),照抄
//    那條路的話「重查」會變成一句對模型的請託,而不是一個真的參數——模型高興
//    就理它,不高興就不理,而且 CSP 那邊**永遠**收不到檢索指令。所以這裡驗的
//    是離開瀏覽器的那個請求上真的有 `X-ANILA-Route: forced`。
//
// 2. **把 steer 文字摻進去**:重查的語意是「同一個問句、原樣重問」。多一句
//    「（重新回答時請依此調整：…）」就等於改了問題,拿回來的答案不再可比,
//    使用者也無從判斷是規章救了他還是那句話救了他。
//
// 3. **無差別加標頭**:四個既有選項與一般送出都不得帶這個標頭。帶了的話,
//    每一輪都會強制檢索,裁決 3(Router 自己判斷要不要搜)就被前端繞過了。
//
// 兩種 target 都要測。指名 agent 的對話直達 CSP(`cspBaseUrl`),Router 的
// 對話走 `routerBaseUrl`;兩條都要能帶標頭。⚠ 測試環境裡兩個 base URL 都是
// 空字串(VITE_* 沒注入),所以**不能**用 URL 分辨兩者——用 payload.model 分:
// `anila-router` = Router 通道,agent id = 直達 CSP 那條。

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

import {
  mountOrchestrator,
  sendText,
  waitForAnswer,
  waitForIdle,
  createFakeBackend,
  screen,
  within,
  fireEvent,
  act,
} from "./helpers/orchestrator.jsx";
import { headerValue } from "./helpers/fakeBackend.js";

const ROUTE_HEADER = "X-ANILA-Route";
const RETRY_LABEL = "改用院內規章重查";
const QUESTION = "差旅費怎麼報?";

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

/** 真正的對話回合(標題產生器打同一個 path,只是 stream:false)。 */
function chatTurns(backend) {
  return backend
    .requestsFor("/v1/chat/completions", "POST")
    .filter((r) => r.body?.stream === true);
}

/** 開重新產生選單並點某一個選項(既有 helper 只點得到「重試（不調整）」)。 */
async function chooseRegenerateOption(label) {
  const triggers = screen.getAllByTitle("重新產生（可選調整方向）");
  await act(async () => {
    fireEvent.click(triggers.at(-1));
  });
  const menu = await screen.findByRole("menu");
  await act(async () => {
    fireEvent.click(within(menu).getByText(label));
  });
}

/** 送出一輪並等到答案落定,回傳假後端。 */
async function firstTurn({ meta = null, answer = "依規定核實報支。" } = {}) {
  const backend = createFakeBackend();
  backend.disableTitleGeneration().enqueueAnswer(answer, meta ? { meta } : {});
  await mountOrchestrator({ backend });
  await sendText(QUESTION);
  await waitForAnswer(answer);
  await waitForIdle();
  return backend;
}

/** 一則訊息的文字(可能是 multimodal parts)。 */
function textOf(message) {
  const content = message?.content;
  if (typeof content === "string") return content;
  return (content || [])
    .filter((p) => p.type === "text")
    .map((p) => p.text)
    .join("");
}

function lastUserText(payload) {
  const users = (payload?.messages || []).filter((m) => m.role === "user");
  return textOf(users.at(-1));
}

describe("改用院內規章重查 — 標頭真的送出去了", () => {
  it("選項在重新產生選單裡", async () => {
    await firstTurn();
    const triggers = screen.getAllByTitle("重新產生（可選調整方向）");
    await act(async () => {
      fireEvent.click(triggers.at(-1));
    });
    const menu = await screen.findByRole("menu");
    expect(within(menu).getByText(RETRY_LABEL)).toBeTruthy();
    // 自製 absolute 往上開會蓋住氣泡正文（空回覆那句會從「自訂調整」兩側露出來）。
    expect(menu.parentElement?.style?.position).toBe("fixed");
  });

  it("按下去的那一次重送帶著 X-ANILA-Route: forced", async () => {
    const backend = await firstTurn();
    backend.enqueueAnswer("依人事管理規則第三條,線上簽核。");

    await chooseRegenerateOption(RETRY_LABEL);
    await waitForIdle();

    const retry = chatTurns(backend).at(-1);
    expect(headerValue(retry, ROUTE_HEADER)).toBe("forced");
  });

  it("Router 對話走的是 Router 通道,標頭照樣在", async () => {
    const backend = await firstTurn();
    backend.enqueueAnswer("依規定辦理。");

    await chooseRegenerateOption(RETRY_LABEL);
    await waitForIdle();

    const retry = chatTurns(backend).at(-1);
    expect(retry.body.model).toBe("anila-router");
    expect(headerValue(retry, ROUTE_HEADER)).toBe("forced");
  });

  it("指名 agent 的對話直達 CSP,標頭也要在", async () => {
    // 這一輪的答案由 agent 產出(`answering_agent_id`),所以 `routedAgentId`
    // 被設起來,重送的 target 變成那個 agent —— app.jsx 的 baseUrl 分流會
    // 走 cspBaseUrl 而不是 routerBaseUrl。Task 6 已釘住:帶標記又指名 agent
    // 的呼叫,CSP 照樣檢索注入,所以這條路是通的。
    const backend = await firstTurn({
      meta: { answering_agent_id: "demo-agent" },
    });
    backend.enqueueAnswer("依規定辦理。");

    await chooseRegenerateOption(RETRY_LABEL);
    await waitForIdle();

    const retry = chatTurns(backend).at(-1);
    expect(retry.body.model).toBe("demo-agent");
    expect(headerValue(retry, ROUTE_HEADER)).toBe("forced");
  });
});

describe("改用院內規章重查 — 同一個問句,原樣重問", () => {
  it("重送的最後一則使用者訊息與原句逐字相同", async () => {
    const backend = await firstTurn();
    backend.enqueueAnswer("依人事管理規則第三條,線上簽核。");

    await chooseRegenerateOption(RETRY_LABEL);
    await waitForIdle();

    expect(lastUserText(chatTurns(backend).at(-1).body)).toBe(QUESTION);
  });

  it("整包 payload 裡沒有任何 steer 的痕跡", async () => {
    // 「重新回答時請依此調整」是既有選項串進 user 訊息的那句話。它只要出現
    // 在重查這一輪,問題就已經被改寫過了。
    const backend = await firstTurn();
    backend.enqueueAnswer("依人事管理規則第三條,線上簽核。");

    await chooseRegenerateOption(RETRY_LABEL);
    await waitForIdle();

    const body = JSON.stringify(chatTurns(backend).at(-1).body);
    expect(body).not.toContain("重新回答時請依此調整");
  });
});

describe("改用院內規章重查 — 不該帶的地方一律不帶", () => {
  it("第一次送出不帶(Router 自己判斷要不要搜,裁決 3)", async () => {
    const backend = await firstTurn();
    expect(headerValue(chatTurns(backend).at(0), ROUTE_HEADER)).toBeUndefined();
  });

  it.each(["重試（不調整）", "更詳細", "更簡潔", "換個說法"])(
    "「%s」不帶",
    async (label) => {
      const backend = await firstTurn();
      backend.enqueueAnswer("換一種說法的答案。");

      await chooseRegenerateOption(label);
      await waitForIdle();

      const retry = chatTurns(backend).at(-1);
      expect(headerValue(retry, ROUTE_HEADER)).toBeUndefined();
    },
  );

  it("重查之後的下一次一般送出,標頭不會黏著", async () => {
    // 旗標若被寫進 state 而不是隨那一次呼叫走,它會從此每一輪都送——
    // 前端就等於單方面關掉了 Router 的判斷。
    const backend = await firstTurn();
    backend.enqueueAnswer("依人事管理規則第三條,線上簽核。");
    await chooseRegenerateOption(RETRY_LABEL);
    await waitForIdle();

    backend.enqueueAnswer("第二個問題的答案。");
    await sendText("那出差呢?");
    await waitForAnswer("第二個問題的答案。");
    await waitForIdle();

    expect(headerValue(chatTurns(backend).at(-1), ROUTE_HEADER)).toBeUndefined();
  });
});
