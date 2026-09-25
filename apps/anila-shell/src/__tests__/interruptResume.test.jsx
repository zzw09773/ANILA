import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

import {
  mountOrchestrator,
  sendText,
  waitForIdle,
  selectConversation,
  createFakeBackend,
  screen,
  waitFor,
  act,
  fireEvent,
} from "./helpers/orchestrator.jsx";
import {
  deltaFrame,
  namedEventFrame,
  metaFrame,
  defaultMeta,
  doneFrame,
  errorFrame,
  headerValue,
} from "./helpers/fakeBackend.js";

const ASK_PAYLOAD = {
  question: "這份報告要多長？",
  options: [
    { label: "重點摘要", value: "summary", description: "一頁以內" },
    { label: "完整報告", value: "full", description: "含附錄" },
  ],
  multi_select: false,
  allow_other: true,
};

const INTERRUPT = {
  interrupt_id: "int-ask-1",
  kind: "ask_user",
  payload: ASK_PAYLOAD,
};

const FOLLOW_UP_INTERRUPT = {
  interrupt_id: "int-ask-2",
  kind: "ask_user",
  payload: {
    ...ASK_PAYLOAD,
    question: "還需要補充哪一項？",
  },
};

const PLAN_INTERRUPT = {
  interrupt_id: "int-plan-1",
  kind: "plan",
  payload: { plan: "1. 檢查資料\n2. 完成報告" },
};

function interruptFrames(interrupt = INTERRUPT, preamble = "請先告訴我篇幅。") {
  return [
    deltaFrame(preamble),
    namedEventFrame("anila.interrupt_requested", interrupt),
    metaFrame(defaultMeta({ interrupt })),
    doneFrame(),
  ];
}

async function waitForPersistedInterrupt(backend, status = "pending") {
  await waitFor(() => {
    const convId = backend.conversationIds()[0];
    const assistant = backend
      .storedMessages(convId)
      .find((m) => m.role === "assistant");
    expect(assistant?.metadata?.interrupt?.interrupt_id).toBeTruthy();
    expect(assistant?.metadata?.interrupt?.status).toBe(status);
  });
}

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("ask_user interrupt — 主聊天流程", () => {
  it("把 object options 畫成 label+description，送出 {selected, other_text}", async () => {
    const backend = createFakeBackend()
      .disableTitleGeneration()
      .enqueueFrames(interruptFrames(), { sessionId: "sess-test-1" })
      .enqueueSessionAnswer("好，依重點摘要繼續。");
    await mountOrchestrator({ backend });

    await sendText("請幫我寫報告");
    expect(await screen.findByText("這份報告要多長？")).toBeTruthy();
    await waitForIdle();
    await waitForPersistedInterrupt(backend);
    expect(screen.getByText("重點摘要")).toBeTruthy();
    expect(screen.getByText("一頁以內")).toBeTruthy();
    expect(screen.getByText("等待您回答")).toBeTruthy();

    fireEvent.click(screen.getByRole("radio", { name: /重點摘要/ }));
    fireEvent.change(screen.getByPlaceholderText(/或輸入其他回應/), {
      target: { value: "主管閱讀、一頁以內" },
    });
    expect(screen.getByRole("radio", { name: /重點摘要/ }).checked).toBe(false);
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "送出回答" }));
    });

    await waitFor(() => expect(backend.sessionAnswers).toHaveLength(1));
    expect(backend.sessionAnswers[0].body).toEqual({
      interrupt_id: "int-ask-1",
      answer: {
        selected: [],
        other_text: "主管閱讀、一頁以內",
      },
    });
    const answerReq = backend.requests.find(
      (req) => req.method === "POST" && req.path.includes("/answer"),
    );
    expect(headerValue(answerReq, "X-ANILA-Conversation-Id")).toBe(
      String(backend.conversationIds()[0]),
    );
    await screen.findByTestId("interrupt-summary");
    expect(screen.getByTestId("interrupt-summary").textContent).toBe(
      "補充：主管閱讀、一頁以內",
    );
    expect(await screen.findByText((t) => t.includes("依重點摘要繼續"))).toBeTruthy();
  });

  it("plan 核准依 Router 契約送出 approved=true", async () => {
    const backend = createFakeBackend()
      .disableTitleGeneration()
      .enqueueFrames(
        interruptFrames(PLAN_INTERRUPT),
        { sessionId: "sess-plan-1" },
      )
      .enqueueSessionAnswer("已依計畫完成。");
    await mountOrchestrator({ backend });

    await sendText("請先規劃報告");
    await screen.findByText(/1\. 檢查資料/);
    await waitForIdle();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "核准計畫" }));
    });

    await waitFor(() => expect(backend.sessionAnswers).toHaveLength(1));
    expect(backend.sessionAnswers[0].body).toEqual({
      interrupt_id: "int-plan-1",
      answer: { approved: true },
    });
    expect(await screen.findByTestId("interrupt-summary")).toBeTruthy();
    expect(screen.getByTestId("interrupt-summary").textContent).toBe("已核准計畫");
  });

  it("reload 後可用已保存的 session id 送出回答", async () => {
    const backend = createFakeBackend()
      .disableTitleGeneration()
      .enqueueFrames(interruptFrames(), { sessionId: "sess-test-1" })
      .enqueueSessionAnswer("好，依重點摘要繼續。");
    const first = await mountOrchestrator({ backend });

    await sendText("請幫我寫報告");
    await screen.findByText("這份報告要多長？");
    await waitForIdle();
    await waitForPersistedInterrupt(backend);
    const convId = backend.conversationIds()[0];
    const persistedAssistant = backend
      .storedMessages(convId)
      .find((message) => message.role === "assistant");
    expect(persistedAssistant?.metadata?.interrupt).toEqual(
      expect.objectContaining({ session_id: "sess-test-1" }),
    );
    first.unmount();

    await mountOrchestrator({ backend });
    await selectConversation("請幫我寫報告");
    await screen.findByText("這份報告要多長？");
    fireEvent.click(screen.getByRole("radio", { name: /重點摘要/ }));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "送出回答" }));
    });

    await waitFor(() => expect(backend.sessionAnswers).toHaveLength(1));
    expect(backend.sessionAnswers[0]).toEqual({
      sessionId: "sess-test-1",
      body: {
        interrupt_id: "int-ask-1",
        answer: { selected: ["summary"], other_text: "" },
      },
    });
  });

  it("續答失敗時保留已選項與補充文字", async () => {
    const backend = createFakeBackend()
      .disableTitleGeneration()
      .enqueueFrames(interruptFrames(), { sessionId: "sess-test-1" })
      .enqueueSessionAnswerError(503, "續答暫時失敗");
    await mountOrchestrator({ backend });

    await sendText("請幫我寫報告");
    await screen.findByText("這份報告要多長？");
    await waitForIdle();
    await waitForPersistedInterrupt(backend);
    fireEvent.click(screen.getByRole("radio", { name: /完整報告/ }));
    fireEvent.change(screen.getByPlaceholderText(/或輸入其他回應/), {
      target: { value: "請保留附錄" },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "送出回答" }));
    });

    expect((await screen.findByTestId("message-stream-error")).textContent).toContain("續答暫時失敗");
    expect(screen.getByRole("radio", { name: /完整報告/ }).checked).toBe(false);
    expect(screen.getByPlaceholderText(/或輸入其他回應/).value).toBe("請保留附錄");
    expect(backend.sessionAnswers[0].body.answer).toEqual({
      selected: [],
      other_text: "請保留附錄",
    });
    expect(screen.getByRole("button", { name: "送出回答" }).disabled).toBe(false);
  });

  it("reload 後仍從 persisted metadata 畫出待回答的問題", async () => {
    const backend = createFakeBackend()
      .disableTitleGeneration()
      .enqueueFrames(interruptFrames(), { sessionId: "sess-test-1" });
    const first = await mountOrchestrator({ backend });

    await sendText("請幫我寫報告");
    await screen.findByText("這份報告要多長？");
    await waitForIdle();
    await waitFor(() => {
      const convId = backend.conversationIds()[0];
      const assistant = backend
        .storedMessages(convId)
        .find((m) => m.role === "assistant");
      expect(assistant?.metadata?.interrupt?.kind).toBe("ask_user");
    });

    first.unmount();
    await mountOrchestrator({ backend });
    await selectConversation("請幫我寫報告");

    expect(await screen.findByText("這份報告要多長？")).toBeTruthy();
    expect(screen.getByRole("radio", { name: /重點摘要/ })).toBeTruthy();
    expect(screen.getByText("等待您回答")).toBeTruthy();
  });

  it("連續兩個 ASK 時，前一題完成不會蓋掉新題", async () => {
    const backend = createFakeBackend()
      .disableTitleGeneration()
      .enqueueFrames(interruptFrames(), { sessionId: "sess-test-1" })
      .enqueueSessionAnswerFrames([
        namedEventFrame("anila.interrupt_requested", FOLLOW_UP_INTERRUPT),
        deltaFrame("收到，接著確認。"),
        metaFrame(defaultMeta({ interrupt: FOLLOW_UP_INTERRUPT })),
        doneFrame(),
      ])
      .enqueueSessionAnswer("最後確認完成。");
    await mountOrchestrator({ backend });

    await sendText("請幫我寫報告");
    await screen.findByText("這份報告要多長？");
    await waitForIdle();
    fireEvent.click(screen.getByRole("radio", { name: /重點摘要/ }));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "送出回答" }));
    });

    expect(await screen.findByText("還需要補充哪一項？")).toBeTruthy();
    await waitFor(() => expect(backend.sessionAnswers).toHaveLength(1));
    const firstSummary = screen.getByTestId("interrupt-summary");
    expect(firstSummary.textContent).toBe("已選擇：重點摘要");
    const followUp = screen.getByText("還需要補充哪一項？");
    expect(firstSummary.compareDocumentPosition(followUp) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByRole("radio", { name: /重點摘要/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: "送出回答" }).disabled).toBe(false);

    fireEvent.click(screen.getByRole("radio", { name: /完整報告/ }));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "送出回答" }));
    });

    await waitFor(() => expect(backend.sessionAnswers).toHaveLength(2));
    expect(backend.sessionAnswers.map(({ body }) => body.interrupt_id)).toEqual([
      "int-ask-1",
      "int-ask-2",
    ]);
    expect(screen.getAllByTestId("interrupt-summary").map((el) => el.textContent)).toEqual([
      "已選擇：重點摘要",
      "已選擇：完整報告",
    ]);
  });

  it("成功續答後的一行摘要會在 reload 後保留", async () => {
    const backend = createFakeBackend()
      .disableTitleGeneration()
      .enqueueFrames(interruptFrames(), { sessionId: "sess-test-1" })
      .enqueueSessionAnswer("好，依重點摘要繼續。");
    const first = await mountOrchestrator({ backend });

    await sendText("請幫我寫報告");
    await screen.findByText("這份報告要多長？");
    await waitForIdle();
    await waitForPersistedInterrupt(backend);
    fireEvent.click(screen.getByRole("radio", { name: /重點摘要/ }));
    fireEvent.change(screen.getByPlaceholderText(/或輸入其他回應/), {
      target: { value: "主管閱讀、一頁以內" },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "送出回答" }));
    });
    await screen.findByTestId("interrupt-summary");
    await waitFor(() => {
      const convId = backend.conversationIds()[0];
      const assistant = backend
        .storedMessages(convId)
        .find((m) => m.role === "assistant");
      expect(assistant?.metadata?.interrupt?.status).toBe("answered");
    });

    first.unmount();
    await mountOrchestrator({ backend });
    await selectConversation("請幫我寫報告");

    expect(await screen.findByTestId("interrupt-summary")).toBeTruthy();
    expect(screen.getByTestId("interrupt-summary").textContent).toBe(
      "補充：主管閱讀、一頁以內",
    );
    expect(screen.queryByRole("button", { name: "送出回答" })).toBeNull();
  });

  it("reload 後 multi 仍在，勾選與補充一起送出，摘要列出全部", async () => {
    const multiInterrupt = {
      interrupt_id: "int-ask-multi",
      kind: "ask_user",
      payload: {
        question: "要挑哪幾個來拆成三種版本？",
        options: [
          { label: "一 環境感測器", value: "sensor", description: "" },
          { label: "六 電網天線", value: "antenna", description: "" },
          { label: "全都要", value: "all", description: "" },
        ],
        multi: true,
        multi_select: false,
        allow_other: true,
      },
    };
    const backend = createFakeBackend()
      .disableTitleGeneration()
      .enqueueFrames(
        interruptFrames(multiInterrupt, "先勾選要拆的項目。"),
        { sessionId: "sess-multi-1" },
      )
      .enqueueSessionAnswer("好，依這幾項拆成三種版本。");
    const first = await mountOrchestrator({ backend });

    await sendText("挑幾個來拆");
    expect(await screen.findByText("要挑哪幾個來拆成三種版本？")).toBeTruthy();
    await waitForIdle();
    await waitForPersistedInterrupt(backend);
    expect(screen.queryByRole("radio")).toBeNull();
    expect(screen.getAllByRole("checkbox").length).toBe(3);
    const convId = backend.conversationIds()[0];
    const pending = backend
      .storedMessages(convId)
      .find((message) => message.role === "assistant");
    expect(pending?.metadata?.interrupt?.payload?.multi).toBe(true);

    first.unmount();
    const second = await mountOrchestrator({ backend });
    await selectConversation("挑幾個來拆");
    expect(await screen.findByText("要挑哪幾個來拆成三種版本？")).toBeTruthy();
    expect(screen.queryByRole("radio")).toBeNull();
    const sensor = screen.getByRole("checkbox", { name: /環境感測器/ });
    const antenna = screen.getByRole("checkbox", { name: /電網天線/ });
    expect(sensor).toBeTruthy();
    expect(antenna).toBeTruthy();
    const reloaded = backend
      .storedMessages(convId)
      .find((message) => message.role === "assistant");
    expect(reloaded?.metadata?.interrupt?.payload?.multi).toBe(true);

    fireEvent.click(sensor);
    fireEvent.click(antenna);
    fireEvent.change(screen.getByPlaceholderText(/或輸入其他回應/), {
      target: { value: "再加備註" },
    });
    expect(sensor.checked).toBe(true);
    expect(antenna.checked).toBe(true);
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "送出回答" }));
    });

    await waitFor(() => expect(backend.sessionAnswers).toHaveLength(1));
    expect(backend.sessionAnswers[0].body).toEqual({
      interrupt_id: "int-ask-multi",
      answer: {
        selected: ["sensor", "antenna"],
        other_text: "再加備註",
      },
    });
    expect(screen.getByTestId("interrupt-summary").textContent).toBe(
      "已選擇：一 環境感測器、六 電網天線；補充：再加備註",
    );
    await waitFor(() => {
      const answered = backend
        .storedMessages(convId)
        .find((message) => message.role === "assistant");
      expect(answered?.metadata?.interrupt?.status).toBe("answered");
      expect(answered?.metadata?.interrupt?.payload?.multi).toBe(true);
    });

    second.unmount();
    await mountOrchestrator({ backend });
    await selectConversation("挑幾個來拆");
    expect(await screen.findByTestId("interrupt-summary")).toBeTruthy();
    expect(screen.getByTestId("interrupt-summary").textContent).toBe(
      "已選擇：一 環境感測器、六 電網天線；補充：再加備註",
    );
    expect(screen.queryByRole("button", { name: "送出回答" })).toBeNull();
  });

  async function submitAsk(label) {
    if (label) {
      fireEvent.click(screen.getByRole("radio", { name: label }));
    }
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "送出回答" }));
    });
    await waitFor(() => expect(screen.getByTestId("interrupt-card")).toBeTruthy());
  }

  it("續答串流的思考增量會即時長出來，結束後不再送出中", async () => {
    const backend = createFakeBackend()
      .disableTitleGeneration()
      .enqueueFrames(interruptFrames(), { sessionId: "sess-think-1" })
      .enqueueSessionAnswerManual();
    backend.route("POST", "/api/thinking/summarize", (req, { jsonResponse }) => {
      const added = String(req.body?.added || "");
      if (!added.includes("續答思考")) return jsonResponse({ summary: null });
      return jsonResponse({ summary: "整理剛才的選擇" });
    });
    await mountOrchestrator({ backend });

    await sendText("請幫我寫報告");
    await screen.findByText("這份報告要多長？");
    await waitForIdle();
    await waitForPersistedInterrupt(backend);
    await submitAsk(/重點摘要/);
    await waitFor(() => expect(backend.sessionAnswers).toHaveLength(1));

    expect(screen.getByTestId("interrupt-summary").textContent).toBe("已選擇：重點摘要");
    expect(screen.queryByRole("button", { name: "送出中…" })).toBeNull();
    expect(screen.queryByRole("button", { name: "送出回答" })).toBeNull();
    expect(screen.getByTestId("interrupt-card").getAttribute("data-interrupt-submitting")).toBe("true");
    expect(screen.getByTestId("thinking-elapsed").textContent).toMatch(/\d+秒/);

    const piece = "續答思考";
    await act(async () => {
      for (let i = 0; i < 100; i += 1) {
        backend.stream.push(namedEventFrame("anila.reasoning", { delta: piece }));
      }
    });
    await waitFor(() => {
      const calls = backend.requestsFor("/api/thinking/summarize", "POST");
      expect(calls.some((call) => String(call.body?.added || "").includes("續答思考"))).toBe(true);
    });
    expect(screen.getByTestId("thinking-summary-headline").textContent).toContain("整理剛才的選擇");
    expect(screen.queryByText(/續答正文/)).toBeNull();

    await act(async () => {
      backend.stream.push(deltaFrame("續答正文"));
    });
    const answer = await screen.findByText(/續答正文/);
    const summary = screen.getByTestId("interrupt-summary");
    const thinking = screen.getByTestId("thinking-summary-headline");
    expect(summary.compareDocumentPosition(thinking) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(thinking.compareDocumentPosition(answer) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByTestId("interrupt-card").getAttribute("data-interrupt-submitting")).toBe("true");

    await act(async () => {
      backend.stream.push(metaFrame(defaultMeta({ trace: [], reasoning: "" })));
      backend.stream.push(doneFrame());
      backend.stream.close();
    });
    await waitFor(() => {
      expect(screen.getByTestId("interrupt-card").getAttribute("data-interrupt-submitting")).toBe("false");
    });
    expect(screen.queryByRole("button", { name: "送出中…" })).toBeNull();
    expect(screen.getByText(/已思考/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "原始思考" }));
    expect(screen.getByText(/續答思考/)).toBeTruthy();
  });

  it("續答串流的 trace 步驟會進到思考折疊", async () => {
    const backend = createFakeBackend()
      .disableTitleGeneration()
      .enqueueFrames(interruptFrames(), { sessionId: "sess-trace-1" })
      .enqueueSessionAnswerManual();
    await mountOrchestrator({ backend });

    await sendText("請幫我寫報告");
    await screen.findByText("這份報告要多長？");
    await waitForIdle();
    await submitAsk(/重點摘要/);
    await waitFor(() => expect(backend.sessionAnswers).toHaveLength(1));
    expect(screen.getByTestId("interrupt-summary").textContent).toBe("已選擇：重點摘要");

    await act(async () => {
      backend.stream.push(namedEventFrame("anila.trace", {
        kind: "thinking",
        label: "整理續答",
        status: "running",
      }));
    });
    expect(screen.getByTestId("thinking-summary-headline").textContent).toMatch(/整理續答/);
    expect(screen.queryByText(/續答完成/)).toBeNull();

    await act(async () => {
      backend.stream.push(deltaFrame("續答完成"));
      backend.stream.push(metaFrame(defaultMeta({ trace: [] })));
      backend.stream.push(doneFrame());
      backend.stream.close();
    });
    expect(await screen.findByText(/續答完成/)).toBeTruthy();
    await waitFor(() => {
      expect(screen.getByTestId("interrupt-card").getAttribute("data-interrupt-submitting")).toBe("false");
    });
    fireEvent.click(screen.getByRole("button", { name: /步分析/ }));
    expect(screen.getByText("整理續答")).toBeTruthy();
  });

  it("續答中途失敗會退出送出中，並還原可再答的卡片", async () => {
    const backend = createFakeBackend()
      .disableTitleGeneration()
      .enqueueFrames(interruptFrames(), { sessionId: "sess-err-1" })
      .enqueueSessionAnswerManual();
    await mountOrchestrator({ backend });

    await sendText("請幫我寫報告");
    await screen.findByText("這份報告要多長？");
    await waitForIdle();
    await waitForPersistedInterrupt(backend);
    fireEvent.click(screen.getByRole("radio", { name: /完整報告/ }));
    fireEvent.change(screen.getByPlaceholderText(/或輸入其他回應/), {
      target: { value: "請保留附錄" },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "送出回答" }));
    });
    await waitFor(() => expect(backend.sessionAnswers).toHaveLength(1));
    expect(screen.getByTestId("interrupt-summary").textContent).toBe("補充：請保留附錄");
    expect(screen.queryByRole("button", { name: "送出中…" })).toBeNull();
    expect(screen.getByTestId("thinking-elapsed")).toBeTruthy();
    expect(screen.getByTestId("interrupt-card").getAttribute("data-interrupt-submitting")).toBe("true");

    await act(async () => {
      backend.stream.push(namedEventFrame("anila.reasoning", { delta: "中途想想" }));
      backend.stream.push(errorFrame({ message: "續答中斷" }));
      backend.stream.close();
    });

    expect(await screen.findByText("續答中斷")).toBeTruthy();
    expect(screen.getByTestId("interrupt-card").getAttribute("data-interrupt-submitting")).toBe("false");
    expect(screen.queryByRole("button", { name: "送出中…" })).toBeNull();
    expect(screen.queryByTestId("thinking-elapsed")).toBeNull();
    expect(screen.queryByTestId("interrupt-summary")).toBeNull();
    expect(screen.getByRole("radio", { name: /完整報告/ }).checked).toBe(false);
    expect(screen.getByPlaceholderText(/或輸入其他回應/).value).toBe("請保留附錄");
    expect(screen.getByRole("button", { name: "送出回答" }).disabled).toBe(false);
    expect(screen.getByTestId("message-stream-error").textContent).toContain("續答中斷");
  });

  it("送出後選項卡立刻收成一行摘要，思考與續文在摘要下面", async () => {
    const moon = {
      interrupt_id: "int-moon",
      kind: "ask_user",
      payload: {
        question: "要去哪？",
        options: [
          { label: "月球中繼", value: "moon", description: "" },
          { label: "地面站", value: "ground", description: "" },
        ],
        multi: true,
        allow_other: true,
      },
    };
    const backend = createFakeBackend()
      .disableTitleGeneration()
      .enqueueFrames(interruptFrames(moon, "先選一個。"), { sessionId: "sess-moon" })
      .enqueueSessionAnswerManual();
    await mountOrchestrator({ backend });

    await sendText("規劃行程");
    await screen.findByText("要去哪？");
    await waitForIdle();
    fireEvent.click(screen.getByRole("checkbox", { name: /月球中繼/ }));
    fireEvent.change(screen.getByPlaceholderText(/或輸入其他回應/), {
      target: { value: "還有你" },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "送出回答" }));
    });
    await waitFor(() => expect(backend.sessionAnswers).toHaveLength(1));

    expect(screen.getByTestId("interrupt-summary").textContent).toBe("已選擇：月球中繼；補充：還有你");
    expect(document.querySelector("input[placeholder*='或輸入其他回應']")).toBeNull();
    expect(screen.queryByRole("button", { name: "送出中…" })).toBeNull();
    expect(screen.queryByRole("checkbox", { name: /月球中繼/ })).toBeNull();
    const summary = screen.getByTestId("interrupt-summary");
    const thinking = screen.getByTestId("thinking-summary-headline");
    expect(thinking.textContent).toMatch(/正在思考|思考中/);
    expect(screen.getByTestId("thinking-elapsed").textContent).toMatch(/\d+秒/);
    expect(summary.compareDocumentPosition(thinking) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    await act(async () => {
      backend.stream.push(deltaFrame("出發。"));
    });
    const streamed = await screen.findByText(/出發/);
    expect(thinking.compareDocumentPosition(streamed) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    await act(async () => {
      backend.stream.push(metaFrame(defaultMeta({ trace: [] })));
      backend.stream.push(doneFrame());
      backend.stream.close();
    });
    await waitFor(() => {
      expect(screen.getByTestId("interrupt-card").getAttribute("data-interrupt-submitting")).toBe("false");
    });
  });

  it("續答失敗後展開原卡，勾選與補充都還在", async () => {
    const moon = {
      interrupt_id: "int-moon",
      kind: "ask_user",
      payload: {
        question: "要去哪？",
        options: [
          { label: "月球中繼", value: "moon", description: "" },
          { label: "地面站", value: "ground", description: "" },
        ],
        multi: true,
        allow_other: true,
      },
    };
    const backend = createFakeBackend()
      .disableTitleGeneration()
      .enqueueFrames(interruptFrames(moon, "先選一個。"), { sessionId: "sess-moon-err" })
      .enqueueSessionAnswerManual();
    await mountOrchestrator({ backend });

    await sendText("規劃行程");
    await screen.findByText("要去哪？");
    await waitForIdle();
    fireEvent.click(screen.getByRole("checkbox", { name: /月球中繼/ }));
    fireEvent.change(screen.getByPlaceholderText(/或輸入其他回應/), {
      target: { value: "還有你" },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "送出回答" }));
    });
    await waitFor(() => expect(backend.sessionAnswers).toHaveLength(1));
    expect(screen.getByTestId("interrupt-summary").textContent).toBe("已選擇：月球中繼；補充：還有你");

    await act(async () => {
      backend.stream.push(errorFrame({ message: "續答中斷" }));
      backend.stream.close();
    });

    expect((await screen.findByTestId("message-stream-error")).textContent).toContain("續答中斷");
    expect(screen.queryByTestId("interrupt-summary")).toBeNull();
    expect(screen.getByRole("checkbox", { name: /月球中繼/ }).checked).toBe(true);
    expect(screen.getByPlaceholderText(/或輸入其他回應/).value).toBe("還有你");
    expect(screen.getByRole("button", { name: "送出回答" }).disabled).toBe(false);
  });

  it("續答還沒結束時重整，已答的中斷顯示摘要而不是選項卡", async () => {
    const backend = createFakeBackend()
      .disableTitleGeneration()
      .enqueueFrames(interruptFrames(), { sessionId: "sess-reload-live" })
      .enqueueSessionAnswerManual();
    const first = await mountOrchestrator({ backend });

    await sendText("請幫我寫報告");
    await screen.findByText("這份報告要多長？");
    await waitForIdle();
    await waitForPersistedInterrupt(backend);
    fireEvent.click(screen.getByRole("radio", { name: /重點摘要/ }));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "送出回答" }));
    });
    await waitFor(() => expect(backend.sessionAnswers).toHaveLength(1));
    await waitFor(() => {
      const convId = backend.conversationIds()[0];
      const assistant = backend.storedMessages(convId).find((m) => m.role === "assistant");
      expect(assistant?.metadata?.interrupt?.status).toBe("answered");
    });

    first.unmount();
    await mountOrchestrator({ backend });
    await selectConversation("請幫我寫報告");

    expect(await screen.findByTestId("interrupt-summary")).toBeTruthy();
    expect(screen.getByTestId("interrupt-summary").textContent).toBe("已選擇：重點摘要");
    expect(screen.queryByRole("radio")).toBeNull();
    expect(screen.queryByRole("button", { name: "送出回答" })).toBeNull();
  });
});
