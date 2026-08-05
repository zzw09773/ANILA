// 先落庫再串流 —— 掛載真正的 ChatRuntime 驅動真正的送出路徑。
//
// 這一組測試的存在理由:前一輪的 442 個測試裡沒有任何一個掛載 App 或
// ChatRuntime,所以把修正的核心那一行刪掉,442 個仍然全綠,而 bug 在瀏覽器
// 裡活得好好的。這裡不用 readFileSync + toContain 檢查原始碼文字 —— 一律
// 掛載元件、打真的送出路徑、對假後端(行為對齊真後端)斷言訊息樹的形狀。

import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, act, waitFor, within } from "@testing-library/react";

import {
  FakeConversationBackend,
  HttpError,
  makeAuthRequest,
} from "./fakeConversationBackend.js";
import { makeStreamWriter } from "../runtime/reservedTurn.js";

// ── 受控串流 ─────────────────────────────────────────────────────────────────
//
// streamChatCompletion 換成手動驅動的假貨:測試可以把串流停在半路,
// 就在那個時候送出第二則訊息 —— 這正是 bug 的觸發條件。

const streamControl = {
  pending: [],
  reset() {
    this.pending = [];
  },
  /** 最近一次(尚未結束的)串流。 */
  latest() {
    return this.pending[this.pending.length - 1];
  },
};

vi.mock("../runtime/sse.js", () => ({
  streamChatCompletion: vi.fn(async (opts) => {
    let resolveDone;
    let rejectDone;
    const done = new Promise((resolve, reject) => {
      resolveDone = resolve;
      rejectDone = reject;
    });
    const handle = {
      opts,
      // 測試呼叫這些來推進串流。
      emit(text) {
        opts.onText?.(text);
      },
      finish() {
        streamControl.pending = streamControl.pending.filter((h) => h !== handle);
        resolveDone();
      },
      /** 模型回報截斷原因(Continue Response 的觸發條件)。 */
      finishReason(reason) {
        opts.onFinishReason?.(reason);
      },
      fail(error) {
        streamControl.pending = streamControl.pending.filter((h) => h !== handle);
        rejectDone(error);
      },
    };
    streamControl.pending.push(handle);
    if (opts.signal) {
      // ⚠ 忠實對齊 runtime/sse.js:153 —— 使用者按停止時它「吞掉 AbortError
      // 並正常返回」,不是拋出。這個假貨原本寫成 reject,結果讓一個真實
      // 缺陷(停止被記成 complete)在 jsdom 裡通過,直到瀏覽器實測才抓到。
      opts.signal.addEventListener("abort", () => {
        handle.finish();
      });
    }
    await done;
  }),
}));

vi.mock("../runtime/tasks.js", () => ({
  createTaskForConversation: vi.fn(async () => null),
  TASK_TITLE_MAX_LENGTH: 60,
}));

let backend;
let authRequest;

// ⚠ 這個物件必須是穩定的(每次 render 回同一個參考)。回新物件的話,
// 任何 depend on authRequest 的 useEffect 每次 render 都會重跑 → setState
// → 再 render,整個元件就進入無窮迴圈(第一次寫這組測試時直接 OOM)。
const AUTH_VALUE = {
  user: { id: 1, username: "tester", role: "user" },
  authReady: true,
  isAuthenticated: true,
  logout: () => {},
  authRequest: (path, options) => authRequest(path, options),
  multipartRequest: () => Promise.resolve({}),
  getCsrfToken: () => null,
};
const LOGOUT_REDIRECT = () => {};

vi.mock("../runtime/auth.jsx", () => ({
  useAuth: () => AUTH_VALUE,
  useLogoutRedirect: () => LOGOUT_REDIRECT,
  AuthProvider: ({ children }) => children,
}));

// 掛載時的雜項端點:回空集合即可,重點不在它們身上。
function fallback(path) {
  if (path.startsWith("/api/message-actions")) return [];
  if (path.startsWith("/api/ui-settings")) return {};
  if (path.startsWith("/api/banners")) return [];
  if (path.startsWith("/v1/agents")) return [];
  return [];
}

const { ChatRuntime } = await import("../app.jsx");
const { ConfirmProvider } = await import("../confirm.jsx");

function renderRuntime() {
  const noop = () => {};
  return render(
    <ConfirmProvider>
      <ChatRuntime
        user={{ id: 1, username: "tester", role: "user" }}
        tweaks={{}}
        setTweaks={noop}
        tweaksOpen={false}
        setTweaksOpen={noop}
      />
    </ConfirmProvider>,
  );
}

/** 打字 + Enter,走真正的 composer(不是直接呼叫內部函式)。 */
async function typeAndSend(text) {
  const textarea = await screen.findByPlaceholderText(/訊息|輸入|問/i);
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLTextAreaElement.prototype,
      "value",
    ).set;
    setter.call(textarea, text);
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await act(async () => {
    textarea.dispatchEvent(
      new KeyboardEvent("keydown", { key: "Enter", bubbles: true, shiftKey: false }),
    );
  });
  return textarea;
}

// jsdom 沒有實作 scrollTo / scrollIntoView(自動捲動用得到)。
if (!window.HTMLElement.prototype.scrollTo) {
  window.HTMLElement.prototype.scrollTo = function scrollTo() {};
}
if (!window.HTMLElement.prototype.scrollIntoView) {
  window.HTMLElement.prototype.scrollIntoView = function scrollIntoView() {};
}

beforeEach(() => {
  backend = new FakeConversationBackend();
  authRequest = makeAuthRequest(backend, { fallback });
  streamControl.reset();
});

afterEach(() => {
  vi.clearAllMocks();
});

// ── 原始事故序列 ─────────────────────────────────────────────────────────────

describe("mid-stream follow-up", () => {
  it("threads Q2 under the reserved A1 — tree is Q1 → A1 → Q2 → A2", async () => {
    renderRuntime();

    await typeAndSend("第一個問題");
    await waitFor(() => expect(streamControl.pending.length).toBe(1));

    // A1 還在串流 —— 這就是使用者按 Enter 插話的時間點。
    await act(async () => {
      streamControl.latest().emit("答案的前半");
    });
    await typeAndSend("插話的第二個問題");

    // 第二輪的串流必須還沒開始(排隊感),但文字已經在伺服器上。
    await waitFor(() => {
      const heads = backend.calls.filter((c) => c.op === "startTurn");
      expect(heads.length).toBe(2);
    });
    expect(streamControl.pending.length).toBe(1);

    // 第一輪跑完 → 第二輪才開始。
    await act(async () => {
      streamControl.latest().emit("第一個答案");
      streamControl.latest().finish();
    });
    await waitFor(() => expect(streamControl.pending.length).toBe(1));
    await act(async () => {
      streamControl.latest().emit("第二個答案");
      streamControl.latest().finish();
    });

    await waitFor(() => {
      const tree = backend.tree(1);
      expect(tree.length).toBe(4);
    });
    const tree = backend.tree(1);
    const [q1, a1, q2, a2] = tree;
    expect(q1[1]).toBe("user");
    expect(a1[1]).toBe("assistant");
    expect(q2[1]).toBe("user");
    expect(a2[1]).toBe("assistant");
    // 形狀:每一則都掛在前一則底下,沒有 user → user。
    expect(a1[2]).toBe(q1[0]);
    expect(q2[2]).toBe(a1[0]);
    expect(a2[2]).toBe(q2[0]);

    // 兩個答案都真的落庫了 —— 這正是原本消失的那一個。
    expect(backend.message(a1[0]).content).toBe("第一個答案");
    expect(backend.message(a2[0]).content).toBe("第二個答案");
  });

  it("reserves the assistant row BEFORE the stream starts", async () => {
    renderRuntime();
    await typeAndSend("第一個問題");
    await waitFor(() => expect(streamControl.pending.length).toBe(1));

    // 串流已經在跑,而使用者訊息和預留列早就一起在伺服器上了。
    const heads = backend.calls.filter((c) => c.op === "startTurn");
    expect(heads.length).toBe(1);
    expect(backend.message(heads[0].reservedId).role).toBe("assistant");
    expect(backend.message(heads[0].reservedId).parent_id).toBe(heads[0].id);
  });

  it("the user's words reach the server before the answer exists", async () => {
    renderRuntime();
    await typeAndSend("我的問題");
    await waitFor(() => {
      const saved = [...backend.messages.values()].find((m) => m.role === "user");
      expect(saved?.content).toBe("我的問題");
    });
  });
});

// ── 串流階段讀的是即時歷史 ───────────────────────────────────────────────────

describe("history for the queued turn", () => {
  it("the second turn sees the first turn's answer, not an empty context", async () => {
    renderRuntime();
    await typeAndSend("第一個問題");
    await waitFor(() => expect(streamControl.pending.length).toBe(1));
    await typeAndSend("第二個問題");

    await act(async () => {
      streamControl.latest().emit("第一個答案");
      streamControl.latest().finish();
    });
    await waitFor(() => expect(streamControl.pending.length).toBe(1));

    const secondPayload = streamControl.latest().opts.payload;
    const roles = secondPayload.messages.map((m) => m.role);
    const contents = secondPayload.messages.map((m) => m.content);
    // 必須看得到第一輪的問與答,而且結尾是這一輪的問題。
    expect(contents).toContain("第一個問題");
    expect(contents).toContain("第一個答案");
    expect(contents[contents.length - 1]).toBe("第二個問題");
    expect(roles[roles.length - 1]).toBe("user");
  });

  it("the queued turn's context excludes messages sent after it", async () => {
    renderRuntime();
    await typeAndSend("第一個問題");
    await waitFor(() => expect(streamControl.pending.length).toBe(1));
    await typeAndSend("第二個問題");
    await typeAndSend("第三個問題");

    const firstPayload = streamControl.pending[0].opts.payload;
    const firstContents = firstPayload.messages.map((m) => m.content);
    // 第一輪的上下文不可以含到後面兩則 —— 它們在時間上還沒發生。
    expect(firstContents).not.toContain("第二個問題");
    expect(firstContents).not.toContain("第三個問題");
    expect(firstContents[firstContents.length - 1]).toBe("第一個問題");

    // 第二輪才是真正的考題:它跑的時候,第三則早就在清單裡了(先落庫的
    // 結果)。歷史必須切在自己的使用者訊息之前,否則會把「還沒發生的
    // 對話」送進模型的上下文。
    await act(async () => {
      streamControl.pending[0].emit("第一個答案");
      streamControl.pending[0].finish();
    });
    await waitFor(() => expect(streamControl.pending.length).toBe(1));

    const secondContents = streamControl
      .latest()
      .opts.payload.messages.map((m) => m.content);
    expect(secondContents).toContain("第一個問題");
    expect(secondContents).toContain("第一個答案");
    expect(secondContents).not.toContain("第三個問題");
    expect(secondContents[secondContents.length - 1]).toBe("第二個問題");
  });
});

// ── 誠實:半截的答案不能長得像完整答案 ───────────────────────────────────────

describe("honesty about partial answers", () => {
  it("a failed stream persists partial text AND marks it incomplete", async () => {
    renderRuntime();
    await typeAndSend("問題");
    await waitFor(() => expect(streamControl.pending.length).toBe(1));

    await act(async () => {
      streamControl.latest().emit("只講到一半");
      streamControl.latest().fail(new Error("連線中斷"));
    });

    await waitFor(() => {
      const assistant = [...backend.messages.values()].find(
        (m) => m.role === "assistant",
      );
      expect(assistant.content).toBe("只講到一半");
      expect(assistant.metadata.anila_stream.state).toBe("failed");
    });
    expect(await screen.findByTestId("message-incomplete-notice")).toBeTruthy();
  });

  it("a completed stream is not marked incomplete", async () => {
    renderRuntime();
    await typeAndSend("問題");
    await waitFor(() => expect(streamControl.pending.length).toBe(1));
    await act(async () => {
      streamControl.latest().emit("完整的答案");
      streamControl.latest().finish();
    });

    await waitFor(() => {
      const assistant = [...backend.messages.values()].find(
        (m) => m.role === "assistant",
      );
      expect(assistant.metadata.anila_stream.state).toBe("complete");
    });
    expect(screen.queryByTestId("message-incomplete-notice")).toBeNull();
  });
});

// ── 紅線:串流期間打字不能掉字、composer 不能被停用 ──────────────────────────

describe("the red line — typing ahead costs nothing", () => {
  it("the composer is never disabled or read-only while streaming", async () => {
    renderRuntime();
    const textarea = await typeAndSend("第一個問題");
    await waitFor(() => expect(streamControl.pending.length).toBe(1));

    await act(async () => {
      for (let i = 0; i < 50; i += 1) streamControl.latest().emit(`重繪 ${i}`);
    });

    expect(textarea.disabled).toBe(false);
    expect(textarea.readOnly).toBe(false);
    expect(textarea.getAttribute("aria-disabled")).not.toBe("true");
  });

  it("keeps every character typed during a heavy re-render", async () => {
    renderRuntime();
    const textarea = await typeAndSend("第一個問題");
    await waitFor(() => expect(streamControl.pending.length).toBe(1));

    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLTextAreaElement.prototype,
      "value",
    ).set;
    const typed = [];
    // 每一次按鍵各自 flush 一次 —— 真實的按鍵之間本來就隔著 event loop。
    // 實測(2026-08-03):這個寫法整份 shell 測試跑完,"Maximum update depth"
    // 出現 0 次;把 300 次同步 setState 塞進同一個 act 則出現 5 次。也就是
    // 那個警告是測試驅動方式的假象,不是元件的迴圈(堆疊顯示 setState 直接
    // 來自 onText,不是來自 useEffect)。
    for (let i = 0; i < 300; i += 1) {
      typed.push(String.fromCharCode(97 + (i % 26)));
      // eslint-disable-next-line no-await-in-loop
      await act(async () => {
        setter.call(textarea, typed.join(""));
        textarea.dispatchEvent(new Event("input", { bubbles: true }));
        // 每打一個字就重繪一次串流,模擬高頻 re-render 下的競爭。
        streamControl.latest().emit(`串流內容 ${i}`);
      });
    }

    expect(textarea.value).toBe(typed.join(""));
    expect(textarea.value.length).toBe(300);
  });
});

// ── 停止、關視窗、兩個分頁 ───────────────────────────────────────────────────

describe("stop mid-stream", () => {
  it("the NEXT answer after a Stop is not labelled as stopped", async () => {
    // 這一條擋的突變:把 app.jsx 串流 finally 裡的
    // `userStoppedRef.current.delete(convId)` 刪掉。426 個測試全綠,但在
    // 瀏覽器裡的行為是:按過一次停止之後,下一則「正常跑完」的答案被寫成
    // stopped,並且掛上「已停止產生，以下是中斷前的內容。」——一個完整的
    // 答案被呈現成半截的。這正是本專案第四條教訓反過來的形態:控制項告訴
    // 使用者發生了一件沒有發生的事。
    //
    // 不變式:跑完的答案永遠不會被標成停止／中斷／失敗。
    //
    // ⚠ 2026-08-05 改寫了情境。原本是「第二則在第一輪還沒結束時送出,
    // 按停止之後第二輪照樣跑完」——那個情境現在不成立了:「停止產生」
    // 已經改成連排隊中的那幾輪一起取消(見下面 "Stop means this
    // conversation stops")。這個測試守的是另一件事(旗標有沒有被清掉),
    // 所以情境改成:先停掉第一輪,等它收乾淨,再送出第二則。
    renderRuntime();
    await typeAndSend("第一個問題");
    await waitFor(() => expect(streamControl.pending.length).toBe(1));
    await act(async () => {
      streamControl.latest().emit("第一個答案被停掉");
    });

    const stop = await screen.findByRole("button", { name: /停止/ });
    await act(async () => {
      stop.click();
    });
    await waitFor(() => {
      const answers = [...backend.messages.values()].filter(
        (m) => m.role === "assistant",
      );
      expect(answers.length).toBe(1);
      expect(answers[0].metadata.anila_stream.state).toBe("stopped");
    });

    // 停止之後才送出的第二則:完全正常地跑完。
    await typeAndSend("第二個問題");
    await waitFor(() => expect(streamControl.pending.length).toBe(1));
    await act(async () => {
      streamControl.latest().emit("第二個答案，完整跑完");
      streamControl.latest().finish();
    });

    await waitFor(() => {
      const answers = [...backend.messages.values()]
        .filter((m) => m.role === "assistant")
        .sort((a, b) => a.id - b.id);
      expect(answers.length).toBe(2);
      expect(answers[1].content).toBe("第二個答案，完整跑完");
      expect(answers[1].metadata.anila_stream.state).toBe("complete");
    });

    // 使用者看到的那一面:只有被停掉的第一則掛著半截標示。
    const notices = await screen.findAllByTestId("message-incomplete-notice");
    expect(notices.length).toBe(1);
    expect(notices[0].textContent).toContain("已停止產生");
  });

  it("keeps the partial text and records it as stopped, not complete", async () => {
    renderRuntime();
    await typeAndSend("問題");
    await waitFor(() => expect(streamControl.pending.length).toBe(1));
    await act(async () => {
      streamControl.latest().emit("講到一半就被停掉");
    });

    // 使用者按下「停止產生」。
    const stop = await screen.findByRole("button", { name: /停止/ });
    await act(async () => {
      stop.click();
    });

    await waitFor(() => {
      const assistant = [...backend.messages.values()].find(
        (m) => m.role === "assistant",
      );
      expect(assistant.metadata.anila_stream.state).toBe("stopped");
      expect(assistant.content).toBe("講到一半就被停掉");
    });
    // 半截的答案一定要標示出來。
    expect(await screen.findByTestId("message-incomplete-notice")).toBeTruthy();
  });
});

describe("the window goes away mid-stream", () => {
  it("marks the reserved row interrupted, and the user's words are already safe", async () => {
    renderRuntime();
    await typeAndSend("關視窗之前的問題");
    await waitFor(() => expect(streamControl.pending.length).toBe(1));
    await act(async () => {
      streamControl.latest().emit("只寫到這裡");
    });

    // 使用者的文字在這一刻就已經在伺服器上了 —— 這是與「留在瀏覽器排隊」
    // 的關鍵差異:關掉視窗不會讓它消失。
    const savedUser = [...backend.messages.values()].find((m) => m.role === "user");
    expect(savedUser.content).toBe("關視窗之前的問題");

    await act(async () => {
      window.dispatchEvent(new Event("pagehide"));
    });

    await waitFor(() => {
      const assistant = [...backend.messages.values()].find(
        (m) => m.role === "assistant",
      );
      expect(assistant.metadata.anila_stream.state).toBe("interrupted");
      expect(assistant.content).toBe("只寫到這裡");
    });
  });

  it("also closes the queued turn's row, not just the one that is streaming", async () => {
    // 插話送出的第二輪:它的預留列已經在伺服器上,但串流還在排隊。
    // pagehide 原本只掃「正在串流」的那些,所以這一列會永遠停在 reserved
    // ——沒有權杖就寫不進去(409),而整個 repo 沒有任何回收程序。
    renderRuntime();
    await typeAndSend("第一個問題");
    await waitFor(() => expect(streamControl.pending.length).toBe(1));
    await act(async () => {
      streamControl.latest().emit("第一個答案的前半");
    });
    await typeAndSend("插話的第二個問題");
    await waitFor(() => {
      expect(backend.calls.filter((c) => c.op === "startTurn").length).toBe(2);
    });
    // 第二輪的串流還沒開始 —— 它就是那個會被漏掉的。
    expect(streamControl.pending.length).toBe(1);

    await act(async () => {
      window.dispatchEvent(new Event("pagehide"));
    });

    await waitFor(() => {
      const answers = [...backend.messages.values()].filter(
        (m) => m.role === "assistant",
      );
      expect(answers.length).toBe(2);
      for (const a of answers) {
        expect(a.metadata.anila_stream.state).toBe("interrupted");
      }
    });
    // 沒有任何一列停在 reserved —— 那種列誰都寫不進去,而且沒有回收程序。
    const stuck = [...backend.messages.values()].filter(
      (m) => m.metadata?.anila_stream?.state === "reserved",
    );
    expect(stuck).toEqual([]);
  });

  it("a row still left non-terminal is shown as unfinished when reloaded", async () => {
    // 模擬「連 pagehide 都沒送出去」的最壞情況:那一列停在 reserved。
    const conv = backend.createConversation({ title: "重整前的對話" });
    const q1 = backend.appendMessage(conv.id, { role: "user", content: "問題" });
    backend.reserveReply(conv.id, q1.id, { stream_writer: "dead-tab-token" });

    renderRuntime();
    const row = await screen.findByText("重整前的對話");
    await act(async () => {
      row.click();
    });

    // 沒有把空白/半截的列裝成完整答案。
    expect(await screen.findByTestId("message-incomplete-notice")).toBeTruthy();
  });

  it("does not call a row that another tab may be writing a crashed one", async () => {
    // 一個人開兩個分頁就會遇到:B 分頁載入時 A 分頁正在寫那一列。
    // 前端分不出「對方正在寫」和「對方掛了」,所以措辭不能斷言連線中斷 ——
    // 那是把一個正在跑的串流報告成當掉的串流。
    const conv = backend.createConversation({ title: "另一個分頁正在寫" });
    const q1 = backend.appendMessage(conv.id, { role: "user", content: "問題" });
    backend.reserveReply(conv.id, q1.id, { stream_writer: "other-tab-token" });

    renderRuntime();
    const row = await screen.findByText("另一個分頁正在寫");
    await act(async () => {
      row.click();
    });

    const notice = await screen.findByTestId("message-incomplete-notice");
    expect(notice.textContent).toContain("還沒有寫完");
    expect(notice.textContent).not.toMatch(/^這則回答沒有產生完成/);
  });
});

// ── 送出失敗:不能開始串流,而且不能說謊 ─────────────────────────────────────

describe("when the turn cannot be sent", () => {
  it("does not start a stream, and both bubbles say so", async () => {
    // 擋的突變:`if (!head.ok)` → `if (false && !head.ok)`。整段失敗分支
    // 對測試而言是死碼,而驗證者證明那條路在實務上走得到(§2 的 409)。
    renderRuntime();
    await typeAndSend("先建立對話");
    await waitFor(() => expect(streamControl.pending.length).toBe(1));
    await act(async () => {
      streamControl.latest().emit("答案");
      streamControl.latest().finish();
    });
    await waitFor(() => expect(streamControl.pending.length).toBe(0));

    // 從這裡開始 head 一律失敗。
    backend.startTurn = () => {
      throw new HttpError(409, "這則使用者訊息已經有回覆，無法重複預留");
    };
    await typeAndSend("送不出去的問題");

    const alerts = await screen.findAllByTestId("message-persist-error");
    expect(alerts.length).toBe(2);
    // 沒有開始串流 —— 沒有位置可以寫,就不該假裝在產生答案。
    expect(streamControl.pending.length).toBe(0);
  });

  it("never tells the user a message was not saved when it might have been", async () => {
    // 不變式:已經落庫的訊息,任何介面都不得被描述成沒有存到。
    // 舊文案是「這則訊息沒有存進對話紀錄…請重新送出一次」——在 reserve
    // 失敗時那是假的(使用者訊息已經在資料庫裡),照著做會貼出兩次。
    renderRuntime();
    await typeAndSend("先建立對話");
    await waitFor(() => expect(streamControl.pending.length).toBe(1));
    await act(async () => {
      streamControl.latest().emit("答案");
      streamControl.latest().finish();
    });
    await waitFor(() => expect(streamControl.pending.length).toBe(0));

    backend.startTurn = () => {
      throw new HttpError(500, "伺服器錯誤");
    };
    await typeAndSend("送不出去的問題");

    const alerts = await screen.findAllByTestId("message-persist-error");
    for (const alert of alerts) {
      expect(alert.textContent).not.toContain("沒有存進對話紀錄");
      // 也不能無條件叫使用者重送 —— 那正是會貼出兩次的那句話。
      expect(alert.textContent).toContain("重新整理");
    }
  });
});

// ── 送出順序 ─────────────────────────────────────────────────────────────────

describe("two Enters in a row", () => {
  it("persists in the order they were sent, even if the first reply is slower", async () => {
    // 擋的突變:讓 chainTurnHead 不再串行(例如 `(key, task) => task()`)。
    // 樹的形狀由伺服器的單一交易保證,所以形狀測試抓不到它;會壞掉的是
    // 「順序」——先按 Enter 的那一則會排到後面去,使用者看得到而且無法自救。
    renderRuntime();
    await typeAndSend("開場白");
    await waitFor(() => expect(streamControl.pending.length).toBe(1));
    await act(async () => {
      streamControl.latest().emit("開場的答案");
      streamControl.latest().finish();
    });
    await waitFor(() => expect(streamControl.pending.length).toBe(0));

    // 第一則的 head 卡在路上,第二則緊接著按 Enter。
    const releaseFirst = backend.holdRequest("turnHead");
    await typeAndSend("先按 Enter 的");
    await typeAndSend("後按 Enter 的");
    await act(async () => {
      releaseFirst();
    });

    await waitFor(() => {
      const users = [...backend.messages.values()].filter((m) => m.role === "user");
      expect(users.length).toBe(3);
    });
    const users = [...backend.messages.values()]
      .filter((m) => m.role === "user")
      .sort((a, b) => a.id - b.id)
      .map((m) => m.content);
    expect(users).toEqual(["開場白", "先按 Enter 的", "後按 Enter 的"]);
  });
});

describe("two tabs on one conversation", () => {
  it("both turns persist, the tree is linear, and neither tab loses anything", async () => {
    const tabA = renderRuntime();
    const tabA_send = async (text) => {
      const ta = await within(tabA.container).findByPlaceholderText(/訊息|輸入|問/i);
      await act(async () => {
        const setter = Object.getOwnPropertyDescriptor(
          window.HTMLTextAreaElement.prototype,
          "value",
        ).set;
        setter.call(ta, text);
        ta.dispatchEvent(new Event("input", { bubbles: true }));
      });
      await act(async () => {
        ta.dispatchEvent(
          new KeyboardEvent("keydown", { key: "Enter", bubbles: true }),
        );
      });
    };

    await tabA_send("A 分頁的問題");
    await waitFor(() => expect(streamControl.pending.length).toBe(1));
    const streamA = streamControl.latest();
    await act(async () => {
      streamA.emit("A 的答案前半");
    });

    // 第二個分頁開起來,選同一個對話。
    const tabB = renderRuntime();
    // ⚠ 只點側欄裡那則對話 —— 用寬鬆的比對會撈到「新對話」按鈕,
    // 那顆按鈕會把 selectedConvId 清成 null,B 分頁就自己開了一個新對話。
    const convRow = await within(tabB.container).findByText("A 分頁的問題");
    await act(async () => {
      convRow.click();
    });

    const taB = await within(tabB.container).findByPlaceholderText(/訊息|輸入|問/i);
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(
        window.HTMLTextAreaElement.prototype,
        "value",
      ).set;
      setter.call(taB, "B 分頁的問題");
      taB.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => {
      taB.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    });

    // B 的訊息掛在 A 預留的那一列底下 —— leaf 沒有在任何人腳下移動。
    await waitFor(() => {
      const userMsgs = [...backend.messages.values()].filter((m) => m.role === "user");
      expect(userMsgs.length).toBe(2);
    });

    // A 分頁把自己的答案寫完:它持有自己那一列的權杖,不會被 B 擋住,
    // 也不會覆蓋到 B 的列。
    await act(async () => {
      streamA.emit("A 的完整答案");
      streamA.finish();
    });
    // ⚠ 這裡原本是 `if (streamB) {...}` 而且只斷言 A 的答案 —— 一個
    // 條件式包住、又沒有斷言的測試是等著爛掉的測試:B 的串流哪天不再
    // 出現,它會靜悄悄地跳過。B 的串流必須存在,B 的答案必須落庫。
    await waitFor(() => {
      expect(streamControl.pending.find((h) => h !== streamA)).toBeTruthy();
    });
    const streamB = streamControl.pending.find((h) => h !== streamA);
    await act(async () => {
      streamB.emit("B 的完整答案");
      streamB.finish();
    });

    await waitFor(() => {
      const tree = backend.tree(1);
      expect(tree.length).toBe(4);
    });
    const tree = backend.tree(1);
    // 線性:user → assistant → user → assistant,沒有 user → user。
    expect(tree.map((t) => t[1])).toEqual([
      "user",
      "assistant",
      "user",
      "assistant",
    ]);
    expect(tree[1][2]).toBe(tree[0][0]);
    expect(tree[2][2]).toBe(tree[1][0]);
    expect(tree[3][2]).toBe(tree[2][0]);
    // 兩個分頁的問題和兩個分頁的答案都還在。
    const contents = [...backend.messages.values()].map((m) => m.content);
    expect(contents).toContain("A 分頁的問題");
    expect(contents).toContain("B 分頁的問題");
    expect(contents).toContain("A 的完整答案");
    expect(contents).toContain("B 的完整答案");
    // 每一則使用者訊息底下都真的有一則答案 —— 沒有人的問題落單。
    for (const [id, role] of tree) {
      if (role !== "user") continue;
      const child = tree.find((t) => t[2] === id);
      expect(child, `訊息 ${id} 沒有任何回答`).toBeTruthy();
      expect(child[1]).toBe("assistant");
    }
  });

  it("two tabs pressing Enter in the same instant still leave no message unanswered", async () => {
    // 驗證者實測「同一瞬間」5 次有 3 次壞掉:A 的 append 之後、reserve
    // 之前,B 的 append 走預設 leaf 掛到 A 的使用者訊息底下,A 的 reserve
    // 隨即 409 —— A 那則使用者訊息永遠拿不到答案,而它明明就在資料庫裡。
    // 現在 append＋reserve 是伺服器端的一個交易,那個窗口不存在。
    // 這裡用 gate 把 A 的請求卡在半路,讓 B 的請求在 A 還沒回來時就送出。
    const tabA = renderRuntime();

    const sendIn = async (container, text) => {
      const ta = await within(container).findByPlaceholderText(/訊息|輸入|問/i);
      await act(async () => {
        const setter = Object.getOwnPropertyDescriptor(
          window.HTMLTextAreaElement.prototype,
          "value",
        ).set;
        setter.call(ta, text);
        ta.dispatchEvent(new Event("input", { bubbles: true }));
      });
      await act(async () => {
        ta.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
      });
    };

    // A 先開一個對話並問第一題,B 選同一個對話。
    await sendIn(tabA.container, "A 的第一題");
    await waitFor(() => expect(streamControl.pending.length).toBe(1));
    await act(async () => {
      streamControl.latest().emit("A 的第一個答案");
      streamControl.latest().finish();
    });
    // B 分頁這時才開起來,選同一個對話(側欄清單在掛載時載入)。
    const tabB = renderRuntime();
    const convRow = await within(tabB.container).findByText("A 的第一題");
    await act(async () => {
      convRow.click();
    });

    // 同一瞬間:A 的 head 卡在路上,B 的 head 在這段期間整個跑完。
    // 伺服器已經做完、回應卡在路上 —— head 若是兩次往返,這正好是那兩次之間。
    const releaseA = backend.holdResponse("turnHead");
    await sendIn(tabA.container, "A 同時送出");
    await sendIn(tabB.container, "B 同時送出");
    await act(async () => {
      releaseA();
    });

    await waitFor(() => {
      const users = [...backend.messages.values()].filter((m) => m.role === "user");
      expect(users.length).toBe(3);
    });

    // 不變式:沒有任何一則使用者訊息落單(user → user 的形狀),
    // 每一則底下都有一個屬於它的助理列。
    const tree = backend.tree(1);
    for (const [id, role] of tree) {
      if (role !== "user") continue;
      const children = tree.filter((t) => t[2] === id);
      expect(children.length, `訊息 ${id} 底下沒有回答`).toBeGreaterThan(0);
      for (const child of children) expect(child[1]).toBe("assistant");
    }
  });
});

// ── 權杖:每一輪各自一把 ──────────────────────────────────────────────────────

describe("the writer token", () => {
  it("is different for every reserved row", async () => {
    // 擋的突變:makeStreamWriter 回固定字串。每個前端拿自己的權杖寫自己的
    // 列,所以形狀測試、落庫測試通通還是綠的 —— 而兩個分頁從此共用同一把
    // 鑰匙,誰都能寫誰的列,整道閘門等於不存在。
    renderRuntime();
    await typeAndSend("第一個問題");
    await waitFor(() => expect(streamControl.pending.length).toBe(1));
    await typeAndSend("插話的第二個問題");
    await waitFor(() => {
      expect(backend.calls.filter((c) => c.op === "startTurn").length).toBe(2);
    });

    const writers = [...backend.messages.values()]
      .map((m) => m.metadata?.anila_stream?.writer)
      .filter(Boolean);
    expect(writers.length).toBe(2);
    expect(new Set(writers).size).toBe(2);
  });

  it("never repeats itself", () => {
    const seen = new Set();
    for (let i = 0; i < 500; i += 1) seen.add(makeStreamWriter());
    expect(seen.size).toBe(500);
  });
});

// ── 跑完的答案不能被自己的前端改掉 ───────────────────────────────────────────

describe("a finished answer is left alone", () => {
  it("pagehide sends nothing for an answer that already completed", async () => {
    // 擋的突變:刪掉串流 finally 裡的
    // `inFlightStreamsRef.current.delete(assistantId)`。兩套測試原本全綠,
    // 而瀏覽器實測是:關分頁時兩則**已經 complete** 的答案被改寫成
    // interrupted —— 使用者看完的答案,重整後變成「沒有產生完成」。
    //
    // ⚠ 斷言看的是「有沒有送出那個請求」,不是「資料庫有沒有被改」。
    // 伺服器現在也擋這種改寫(終局後狀態封存,409),所以只斷言資料庫的話,
    // 前端的 bug 會被伺服器救掉而測不出來。兩道防線要各自測得到。
    renderRuntime();
    await typeAndSend("問題");
    await waitFor(() => expect(streamControl.pending.length).toBe(1));
    await act(async () => {
      streamControl.latest().emit("完整的答案");
      streamControl.latest().finish();
    });
    await waitFor(() => {
      const a = [...backend.messages.values()].find((m) => m.role === "assistant");
      expect(a.metadata.anila_stream.state).toBe("complete");
    });

    const attemptsBefore = backend.updateAttempts.length;
    await act(async () => {
      window.dispatchEvent(new Event("pagehide"));
    });
    expect(backend.updateAttempts.length).toBe(attemptsBefore);

    const a = [...backend.messages.values()].find((m) => m.role === "assistant");
    expect(a.metadata.anila_stream.state).toBe("complete");
    expect(a.content).toBe("完整的答案");
    expect(screen.queryByTestId("message-incomplete-notice")).toBeNull();
  });

  it("the server refuses the downgrade even if a client asks for it", async () => {
    // 第二道防線,獨立於前端:終局後 writer 權杖已經清掉,所以那一列對
    // 「不帶權杖的 PUT」是開放的 —— terminal→terminal 原本沒有任何閘門。
    renderRuntime();
    await typeAndSend("問題");
    await waitFor(() => expect(streamControl.pending.length).toBe(1));
    await act(async () => {
      streamControl.latest().emit("完整的答案");
      streamControl.latest().finish();
    });
    await waitFor(() => {
      const a = [...backend.messages.values()].find((m) => m.role === "assistant");
      expect(a.metadata.anila_stream.state).toBe("complete");
    });
    const a = [...backend.messages.values()].find((m) => m.role === "assistant");

    expect(() =>
      backend.updateMessage(a.conversation_id, a.id, {
        metadata: { anila_stream: { state: "interrupted" } },
      }),
    ).toThrow(/已經結束/);
    expect(backend.message(a.id).metadata.anila_stream.state).toBe("complete");
  });

  it("an unrelated metadata patch does not erase the partial marker", async () => {
    renderRuntime();
    await typeAndSend("問題");
    await waitFor(() => expect(streamControl.pending.length).toBe(1));
    await act(async () => {
      streamControl.latest().emit("只講到一半");
      streamControl.latest().fail(new Error("連線中斷"));
    });
    await waitFor(() => {
      const a = [...backend.messages.values()].find((m) => m.role === "assistant");
      expect(a.metadata.anila_stream.state).toBe("failed");
    });
    const a = [...backend.messages.values()].find((m) => m.role === "assistant");

    backend.updateMessage(a.conversation_id, a.id, {
      metadata: { citations: [{ title: "來源" }] },
    });
    expect(backend.message(a.id).metadata.anila_stream.state).toBe("failed");
  });
});

// ── 未知狀態:封閉性,不是列舉 ───────────────────────────────────────────────

describe("a state we do not recognise", () => {
  it("is never rendered as a complete answer", async () => {
    // 擋的突變:把 streamStateNotice 的 default 分支改回 null(或把伺服器
    // 那道未知 state 的 400 拿掉)。列舉已知的壞狀態、其他都當成好的,就是
    // 黑名單 —— 黑名單永遠補不完,而漏掉的那一個會渲染得跟完整答案一樣。
    const conv = backend.createConversation({ title: "狀態不明的對話" });
    const q1 = backend.appendMessage(conv.id, { role: "user", content: "問題" });
    backend.appendMessage(conv.id, {
      role: "assistant",
      content: "看起來很完整的答案",
      parent_id: q1.id,
      metadata: { anila_stream: { state: "weird-new-state" } },
    });

    renderRuntime();
    const row = await screen.findByText("狀態不明的對話");
    await act(async () => {
      row.click();
    });

    const notice = await screen.findByTestId("message-incomplete-notice");
    expect(notice.textContent).toContain("無法辨識");
  });

  it("cannot be written through the update path at all", () => {
    const conv = backend.createConversation({ title: "x" });
    const q1 = backend.appendMessage(conv.id, { role: "user", content: "問題" });
    const a1 = backend.reserveReply(conv.id, q1.id, { stream_writer: "tok-12345678" });
    expect(() =>
      backend.updateMessage(conv.id, a1.id, {
        content: "半截",
        stream_writer: "tok-12345678",
        metadata: { anila_stream: { state: "definitely-fine" } },
      }),
    ).toThrow(/不合法/);
    expect(backend.message(a1.id).content).toBe("");
  });
});

// ── 編輯重問:同一個保證,另一條路徑 ─────────────────────────────────────────

async function editFirstUserMessage(from, to) {
  const editButton = await screen.findByTitle("編輯");
  await act(async () => {
    editButton.click();
  });
  const setter = Object.getOwnPropertyDescriptor(
    window.HTMLTextAreaElement.prototype,
    "value",
  ).set;
  const editArea = [...document.querySelectorAll("textarea")].find(
    (t) => t.value === from,
  );
  expect(editArea, "找不到編輯中的輸入框").toBeTruthy();
  await act(async () => {
    setter.call(editArea, to);
    editArea.dispatchEvent(new Event("input", { bubbles: true }));
  });
  const submit = [...document.querySelectorAll("button")].find(
    (b) => b.textContent.trim() === "送出",
  );
  await act(async () => {
    submit.click();
  });
}

describe("edit and re-ask", () => {
  it("reserves the answer BEFORE streaming, so the leaf never sits on a user row",
    async () => {
      renderRuntime();
      await typeAndSend("ER1 原問題");
      await waitFor(() => expect(streamControl.pending.length).toBe(1));
      await act(async () => {
        streamControl.latest().emit("原問題的答案");
        streamControl.latest().finish();
      });
      await waitFor(() => expect(streamControl.pending.length).toBe(0));

      await editFirstUserMessage("ER1 原問題", "ER2 編輯後的問題");

      await waitFor(() => {
        expect(backend.calls.filter((c) => c.op === "branchTurn").length).toBe(1);
      });
      const head = backend.calls.find((c) => c.op === "branchTurn");
      // 串流已經在跑,而助理列早就在伺服器上了。
      await waitFor(() => expect(streamControl.pending.length).toBe(1));
      expect(backend.message(head.reservedId).role).toBe("assistant");
      expect(backend.message(head.reservedId).parent_id).toBe(head.id);
      // active leaf 落在助理列上 —— 不是編輯後那則使用者訊息。
      expect(backend.conversations.get(1).active_leaf_message_id).toBe(
        head.reservedId,
      );
    });

  it("a mid-stream follow-up cannot orphan the edited question's answer", async () => {
    // 驗證者 2026-08-05 在瀏覽器裡實測到的事故序列。舊流程的樹是
    //   #2530 user 'ER2 edited question' → #2531 user 'ER3 followup'
    //   → #2532 assistant
    // 而 ER2 的答案被 400「分支訊息的角色必須與既有子訊息相同」擋掉,
    // 重整之後就沒了 —— 它一秒鐘前還完整地顯示在螢幕上。
    renderRuntime();
    await typeAndSend("ER1 原問題");
    await waitFor(() => expect(streamControl.pending.length).toBe(1));
    await act(async () => {
      streamControl.latest().emit("原問題的答案");
      streamControl.latest().finish();
    });
    await waitFor(() => expect(streamControl.pending.length).toBe(0));

    await editFirstUserMessage("ER1 原問題", "ER2 編輯後的問題");
    await waitFor(() => expect(streamControl.pending.length).toBe(1));
    await act(async () => {
      streamControl.latest().emit("編輯後那題的答案前半");
    });

    // 串流中途插話 —— 這就是事故的觸發點。
    await typeAndSend("ER3 插話");
    await waitFor(() => {
      expect(backend.calls.filter((c) => c.op === "startTurn").length).toBe(2);
    });

    await act(async () => {
      streamControl.latest().emit("編輯後那題的完整答案");
      streamControl.latest().finish();
    });
    await waitFor(() => expect(streamControl.pending.length).toBe(1));
    await act(async () => {
      streamControl.latest().emit("插話的答案");
      streamControl.latest().finish();
    });
    await waitFor(() => expect(streamControl.pending.length).toBe(0));

    const rows = [...backend.messages.values()];
    const byId = new Map(rows.map((m) => [m.id, m]));
    // 沒有 user → user 這種邊。
    for (const m of rows) {
      if (m.role !== "user" || m.parent_id == null) continue;
      expect(byId.get(m.parent_id).role, `訊息 ${m.id} 掛在使用者訊息底下`)
        .not.toBe("user");
    }
    // 每一則使用者訊息底下都有一則屬於它的助理回答。
    for (const m of rows) {
      if (m.role !== "user") continue;
      const children = rows.filter((c) => c.parent_id === m.id);
      expect(children.length, `訊息 ${m.id} 沒有任何回答`).toBeGreaterThan(0);
      for (const child of children) expect(child.role).toBe("assistant");
    }
    // 三個答案都落庫了 —— 包含原本會消失的那一個。
    const contents = rows.map((m) => m.content);
    expect(contents).toContain("原問題的答案");
    expect(contents).toContain("編輯後那題的完整答案");
    expect(contents).toContain("插話的答案");
    // 沒有任何一列停在 reserved。
    expect(
      rows.filter((m) => m.metadata?.anila_stream?.state === "reserved"),
    ).toEqual([]);
  });
});

// ── 「停止產生」要停得掉排隊中的那幾輪 ───────────────────────────────────────

describe("Stop means this conversation stops", () => {
  it("cancels the turns that have not started streaming yet", async () => {
    // 原本 stopStreaming 只 abort 當下註冊的那一個 controller:在第一輪按
    // 停止,第二輪立刻接著開始跑,而按鈕仍然顯示「停止產生」。使用者按了、
    // 畫面照樣在動 —— 控制項讓使用者以為發生了一件沒有發生的事。
    renderRuntime();
    await typeAndSend("第一個問題");
    await waitFor(() => expect(streamControl.pending.length).toBe(1));
    await act(async () => {
      streamControl.latest().emit("第一個答案的前半");
    });
    await typeAndSend("排在後面的第二個問題");
    await waitFor(() => {
      expect(backend.calls.filter((c) => c.op === "startTurn").length).toBe(2);
    });
    expect(streamControl.pending.length).toBe(1);

    const stop = await screen.findByRole("button", { name: /停止/ });
    await act(async () => {
      stop.click();
    });

    await waitFor(() => {
      const answers = [...backend.messages.values()]
        .filter((m) => m.role === "assistant")
        .sort((a, b) => a.id - b.id);
      expect(answers.length).toBe(2);
      expect(answers[0].metadata.anila_stream.state).toBe("stopped");
      expect(answers[1].metadata.anila_stream.state).toBe("stopped");
    });
    // 排隊中那一輪的串流從來沒有開始。
    expect(streamControl.pending.length).toBe(0);

    const answers = [...backend.messages.values()]
      .filter((m) => m.role === "assistant")
      .sort((a, b) => a.id - b.id);
    expect(answers[0].content).toBe("第一個答案的前半");
    expect(answers[1].content).toBe("");
    // 沒有任何一列停在 reserved —— 那種列誰都寫不進去,也沒有回收程序。
    expect(
      [...backend.messages.values()].filter(
        (m) => m.metadata?.anila_stream?.state === "reserved",
      ),
    ).toEqual([]);

    // 使用者看到的那一面:沒有內容的那一則不能說「以下是中斷前的內容」。
    const notices = await screen.findAllByTestId("message-incomplete-notice");
    expect(notices.length).toBe(2);
    expect(notices.map((n) => n.textContent).join("|")).toContain(
      "沒有留下任何內容",
    );
  });
});

// ── Continue Response:續寫要真的寫回去 ──────────────────────────────────────

describe("Continue Response", () => {
  it("persists the continuation instead of losing it on reload", async () => {
    // 2026-08-05 之前這條路徑完全沒有落庫:續寫在螢幕上長出來、沒有任何
    // 錯誤,而資料庫裡那一列仍然只有續寫前的文字。使用者看著答案變長,
    // 重新整理就沒了。(不是先落庫再串流引入的缺陷。)
    renderRuntime();
    await typeAndSend("問題");
    await waitFor(() => expect(streamControl.pending.length).toBe(1));
    await act(async () => {
      streamControl.latest().emit("被截斷的前半");
      streamControl.latest().finishReason("length");
      streamControl.latest().finish();
    });
    await waitFor(() => {
      const a = [...backend.messages.values()].find((m) => m.role === "assistant");
      expect(a.content).toBe("被截斷的前半");
    });

    const cont = await screen.findByRole("button", { name: /繼續產生/ });
    await act(async () => {
      cont.click();
    });
    await waitFor(() => expect(streamControl.pending.length).toBe(1));
    await act(async () => {
      streamControl.latest().emit("後半段");
      streamControl.latest().finish();
    });

    await waitFor(() => {
      const a = [...backend.messages.values()].find((m) => m.role === "assistant");
      expect(a.content).toBe("被截斷的前半 後半段");
    });
    // 續寫沒有把那一列的串流標記弄丟。
    const a = [...backend.messages.values()].find((m) => m.role === "assistant");
    expect(a.metadata.anila_stream.state).toBe("complete");
  });
});

// ── 卡住的預留列:使用者面前要有出口 ─────────────────────────────────────────

describe("a reserved row nobody can write", () => {
  it("still offers the regenerate control", async () => {
    // 分頁被作業系統殺掉,pagehide 送不出去 —— 那一列永遠停在 reserved。
    // 它寫不進去(409),整個 repo 也沒有任何回收程序。原本連動作列都不畫
    // (text 和 error 都是空的),使用者面前只剩一顆什麼都沒有的氣泡。
    const conv = backend.createConversation({ title: "分頁被殺掉的對話" });
    const q1 = backend.appendMessage(conv.id, { role: "user", content: "問題" });
    backend.reserveReply(conv.id, q1.id, { stream_writer: "dead-tab-token" });

    renderRuntime();
    const row = await screen.findByText("分頁被殺掉的對話");
    await act(async () => {
      row.click();
    });

    expect(await screen.findByTestId("message-incomplete-notice")).toBeTruthy();
    const regen = await screen.findByTitle(/重新產生/);
    expect(regen).toBeTruthy();
    expect(regen.disabled).toBe(false);
  });
});

describe("edit and re-ask — no duplicate bubble", () => {
  it("renders exactly one answer after the active path is refreshed", async () => {
    // 舊缺陷(dupReplyReconcile.test.js 記錄的那一個):樂觀氣泡在
    // refreshActivePath 讀清單時還沒有 dbId,applyServerPath 就把它當成
    // pending tail 留著 —— 畫面上兩則一模一樣的回答,直到 F5 才消失。
    // 先落庫再串流之後 dbId 在串流開始「之前」就有了,但這件事仍然要有
    // 行為面的守衛,而不是只靠原始碼字串比對。
    renderRuntime();
    await typeAndSend("原問題");
    await waitFor(() => expect(streamControl.pending.length).toBe(1));
    await act(async () => {
      streamControl.latest().emit("原問題的答案");
      streamControl.latest().finish();
    });
    await waitFor(() => expect(streamControl.pending.length).toBe(0));

    await editFirstUserMessage("原問題", "編輯後的問題");
    await waitFor(() => expect(streamControl.pending.length).toBe(1));
    await act(async () => {
      streamControl.latest().emit("編輯後那題的答案");
      streamControl.latest().finish();
    });
    await waitFor(() => expect(streamControl.pending.length).toBe(0));

    await waitFor(() => {
      expect(screen.getAllByText("編輯後那題的答案").length).toBe(1);
    });
    expect(screen.getAllByText("編輯後的問題").length).toBe(1);
    // 被放棄的那一支不在 active path 上。
    expect(screen.queryByText("原問題的答案")).toBeNull();
  });
});

describe("a queued turn survives a path refresh", () => {
  it("keeps streaming into the same bubble after edit-and-re-ask refreshes", async () => {
    // 2026-08-05 真 Chrome 實測抓到的:編輯重問結束時會 refreshActivePath,
    // 而那時排隊中那一輪的預留列已經在伺服器上(state=reserved)。
    // applyServerPath 照單全收的話,本地氣泡被換成伺服器版本(另一個 id),
    // 之後串流的每一次 updateMsg 都打在不存在的 id 上 —— 畫面停在
    // 「這則回答還沒有寫完」,一個字都不再出現,直到使用者重整。
    renderRuntime();
    await typeAndSend("原問題");
    await waitFor(() => expect(streamControl.pending.length).toBe(1));
    await act(async () => {
      streamControl.latest().emit("原問題的答案");
      streamControl.latest().finish();
    });
    await waitFor(() => expect(streamControl.pending.length).toBe(0));

    await editFirstUserMessage("原問題", "編輯後的問題");
    await waitFor(() => expect(streamControl.pending.length).toBe(1));
    await act(async () => {
      streamControl.latest().emit("編輯後那題的答案前半");
    });

    // 插話 —— 它的預留列已經在伺服器上,但串流還在排隊。
    await typeAndSend("插話的問題");
    await waitFor(() => {
      expect(backend.calls.filter((c) => c.op === "startTurn").length).toBe(2);
    });

    // 編輯那一輪跑完 → refreshActivePath 會在這裡發生。
    await act(async () => {
      streamControl.latest().emit("編輯後那題的答案");
      streamControl.latest().finish();
    });
    await waitFor(() => expect(streamControl.pending.length).toBe(1));
    await act(async () => {
      streamControl.latest().emit("插話的答案");
      streamControl.latest().finish();
    });
    await waitFor(() => expect(streamControl.pending.length).toBe(0));

    // 使用者看得到插話的答案,而且沒有卡在「還沒有寫完」。
    await waitFor(() => {
      expect(screen.getAllByText("插話的答案").length).toBe(1);
    });
    const notices = screen.queryAllByTestId("message-incomplete-notice");
    expect(notices.map((n) => n.textContent).join("|")).not.toContain("還沒有寫完");
  });
});
