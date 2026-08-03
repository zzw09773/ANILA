// 先落庫再串流 —— 掛載真正的 ChatRuntime 驅動真正的送出路徑。
//
// 這一組測試的存在理由:前一輪的 442 個測試裡沒有任何一個掛載 App 或
// ChatRuntime,所以把修正的核心那一行刪掉,442 個仍然全綠,而 bug 在瀏覽器
// 裡活得好好的。這裡不用 readFileSync + toContain 檢查原始碼文字 —— 一律
// 掛載元件、打真的送出路徑、對假後端(行為對齊真後端)斷言訊息樹的形狀。

import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, act, waitFor, within } from "@testing-library/react";

import { FakeConversationBackend, makeAuthRequest } from "./fakeConversationBackend.js";

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
      const appends = backend.calls.filter(
        (c) => c.op === "appendMessage" && c.role === "user",
      );
      expect(appends.length).toBe(2);
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

    const ops = backend.calls.map((c) => c.op);
    const reserveAt = ops.indexOf("reserveReply");
    const userAppendAt = ops.indexOf("appendMessage");
    expect(userAppendAt).toBeGreaterThanOrEqual(0);
    expect(reserveAt).toBeGreaterThan(userAppendAt);
    // 串流已經在跑,而預留早就完成了 —— 順序反過來就是原本的 bug。
    expect(backend.calls[reserveAt].op).toBe("reserveReply");
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
    // 把 300 次同步 setState 塞進同一個 act,React 會把它們算成一條巢狀
    // 更新鏈而噴 "Maximum update depth"(那是測試驅動方式的假象,不是
    // 元件的迴圈:堆疊顯示 setState 直接來自 onText,不是來自 useEffect)。
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
    const streamB = streamControl.pending.find((h) => h !== streamA);
    if (streamB) {
      await act(async () => {
        streamB.emit("B 的完整答案");
        streamB.finish();
      });
    }

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
    // 兩個分頁的問題都還在。
    const contents = [...backend.messages.values()].map((m) => m.content);
    expect(contents).toContain("A 分頁的問題");
    expect(contents).toContain("B 分頁的問題");
    expect(contents).toContain("A 的完整答案");
  });
});
