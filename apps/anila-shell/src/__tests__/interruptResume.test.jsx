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

function interruptFrames(preamble = "請先告訴我篇幅。") {
  return [
    deltaFrame(preamble),
    namedEventFrame("anila.interrupt_requested", INTERRUPT),
    metaFrame(defaultMeta()),
    doneFrame(),
  ];
}

async function waitForPersistedInterrupt(backend, status = "pending") {
  await waitFor(() => {
    const convId = backend.conversationIds()[0];
    const assistant = backend
      .storedMessages(convId)
      .find((m) => m.role === "assistant");
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
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "送出回答" }));
    });

    await waitFor(() => expect(backend.sessionAnswers).toHaveLength(1));
    expect(backend.sessionAnswers[0].body).toEqual({
      interrupt_id: "int-ask-1",
      answer: {
        selected: ["summary"],
        other_text: "主管閱讀、一頁以內",
      },
    });
    await screen.findByTestId("interrupt-summary");
    expect(screen.getByTestId("interrupt-summary").textContent).toBe(
      "已選擇：重點摘要；補充：主管閱讀、一頁以內",
    );
    expect(await screen.findByText((t) => t.includes("依重點摘要繼續"))).toBeTruthy();
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

    await screen.findByText("續答暫時失敗");
    expect(screen.getByRole("radio", { name: /完整報告/ }).checked).toBe(true);
    expect(screen.getByPlaceholderText(/或輸入其他回應/).value).toBe("請保留附錄");
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
      "已選擇：重點摘要；補充：主管閱讀、一頁以內",
    );
    expect(screen.queryByRole("button", { name: "送出回答" })).toBeNull();
  });
});
