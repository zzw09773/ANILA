import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { cleanup, fireEvent, waitFor, act } from "@testing-library/react";

import {
  mountOrchestrator,
  sendText,
  waitForIdle,
  composerBox,
  createFakeBackend,
  screen,
} from "./helpers/orchestrator.jsx";
import { errorFrame, scriptAnswer } from "./helpers/fakeBackend.js";
import { OVERLOAD_REJECTION_COPY } from "../runtime/pendingReply.js";

const QUOTA = "已達使用者每日 token 上限（10,000），將於 2026-10-02 00:00（台北時間）重置。";

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function turnPosts(backend) {
  return backend.requestsFor(/\/turn$/, "POST");
}

async function pressEnter(text) {
  const box = composerBox();
  fireEvent.change(box, { target: { value: text } });
  await act(async () => {
    fireEvent.keyDown(box, { key: "Enter", code: "Enter" });
  });
}

describe("回覆還沒回來時，同一段文字不再佔排隊位置", () => {
  it("重送同一句不打下一輪，換一句仍會送出", async () => {
    const backend = createFakeBackend();
    backend.disableTitleGeneration().enqueueManualStream().enqueueAnswer("第二則回答");
    await mountOrchestrator({ backend });

    await sendText("同樣的問題");
    await waitFor(() => {
      expect(screen.getByLabelText("停止產生")).toBeTruthy();
    });
    const turns = turnPosts(backend).length;
    const chats = backend.chatPayloads.length;
    expect(turns).toBeGreaterThan(0);

    await pressEnter("同樣的問題");
    expect(screen.getByTestId("duplicate-send-notice").textContent).toContain("請先不要重送");
    expect(screen.getByTestId("duplicate-send-notice").textContent).not.toContain("請稍後再試");
    expect(screen.getByTestId("duplicate-send-notice").textContent).not.toMatch(/分鐘/);
    expect(composerBox().value).toBe("同樣的問題");
    expect(turnPosts(backend).length).toBe(turns);
    expect(backend.chatPayloads.length).toBe(chats);

    await pressEnter("  同樣的問題  ");
    expect(turnPosts(backend).length).toBe(turns);

    await pressEnter("另一個問題");
    await waitFor(() => {
      expect(turnPosts(backend).length).toBe(turns + 1);
    });
    expect(backend.chatPayloads.length).toBe(chats);
  });

  it("這一輪結束後，同一句可以再送", async () => {
    const backend = createFakeBackend();
    backend.disableTitleGeneration().enqueueManualStream().enqueueAnswer("再答一次");
    await mountOrchestrator({ backend });

    await sendText("同樣的問題");
    await waitFor(() => expect(backend.stream).toBeTruthy());
    const turns = turnPosts(backend).length;
    await act(async () => {
      backend.stream.pushAll(scriptAnswer("答完了"));
      backend.stream.close();
    });
    await waitForIdle();

    await sendText("同樣的問題");
    await waitFor(() => {
      expect(turnPosts(backend).length).toBe(turns + 1);
    });
    expect(screen.queryByTestId("duplicate-send-notice")).toBeNull();
  });
});

describe("過載或 429 被拒時留下原文", () => {
  it("HTTP 429 把原文留在輸入框，並改用請先不要重送", async () => {
    const backend = createFakeBackend();
    backend.disableTitleGeneration().enqueueHttpError(429, "overloaded");
    await mountOrchestrator({ backend });

    await sendText("被拒絕的問題");
    await waitForIdle();

    expect(composerBox().value).toBe("被拒絕的問題");
    const alert = await screen.findByTestId("message-stream-error");
    expect(alert.textContent).toBe(OVERLOAD_REJECTION_COPY);
    expect(alert.textContent).not.toContain("請稍後再試");
    expect(alert.textContent).not.toMatch(/分鐘/);
  });

  it("排隊逾時不顯示猜不到的等待，也不寫請稍後再試", async () => {
    const backend = createFakeBackend();
    backend.disableTitleGeneration().enqueueFrames([
      errorFrame({
        code: "model_queue_timeout",
        message: "排隊超過 120 秒，請稍後再試",
      }),
    ]);
    await mountOrchestrator({ backend });

    await sendText("排很久的問題");
    await waitForIdle();

    expect(composerBox().value).toBe("排很久的問題");
    const alert = await screen.findByTestId("message-stream-error");
    expect(alert.textContent).toBe(OVERLOAD_REJECTION_COPY);
    expect(alert.textContent).not.toMatch(/120/);
    expect(alert.textContent).not.toContain("請稍後再試");
  });

  it("額度 429 仍顯示用量原文，不把草稿塞回輸入框", async () => {
    const backend = createFakeBackend();
    backend.disableTitleGeneration().enqueueHttpError(429, QUOTA);
    await mountOrchestrator({ backend });

    await sendText("超過額度的問題");
    await waitForIdle();

    expect(composerBox().value).toBe("");
    const quota = await screen.findByTestId("quota-exceeded");
    expect(quota.textContent).toContain(QUOTA);
    expect(quota.textContent).not.toContain("請先不要重送");
    expect(screen.getByTestId("message-stream-error").textContent).toBe(QUOTA);
  });
});
