// 掛載**真的** orchestrator。
//
// 這裡 render 的是 `src/app.jsx` 的 default export,包在 `main.jsx` 用的
// 同一組 Provider 裡(AuthProvider + ConfirmProvider)。AuthProvider 也是
// 真貨 — 它自己去打 `/api/auth/me`,由假後端回答,所以連「登入態怎麼進來的」
// 都走真的路徑。
//
// ⚠ 這個檔案不准出現 app.jsx 裡任何邏輯的重寫版。一旦某個 orchestration
// 行為在這裡被「重新實作一遍」,測試就會變成在測 harness 自己——那正是
// 「測試全綠但產品壞掉」的成因。

import React from "react";
import { render, screen, waitFor, within, fireEvent, act } from "@testing-library/react";
import { vi, expect } from "vitest";

import App from "../../app.jsx";
import { AuthProvider } from "../../runtime/auth.jsx";
import { ConfirmProvider } from "../../confirm.jsx";
import { createFakeBackend } from "./fakeBackend.js";

/**
 * 掛載 orchestrator,等到 composer 出現(= 登入態已就緒、首批載入結束)。
 *
 * @param {object} [opts]
 * @param {ReturnType<typeof createFakeBackend>} [opts.backend]
 * @returns {Promise<{ backend, ...RenderResult }>}
 */
export async function mountOrchestrator({ backend, ...backendOptions } = {}) {
  const be = backend || createFakeBackend(backendOptions);
  vi.stubGlobal("fetch", vi.fn(be.fetch));

  const utils = render(
    <AuthProvider>
      <ConfirmProvider>
        <App />
      </ConfirmProvider>
    </AuthProvider>,
  );

  // composer 在的時候,ChatRuntime 已經真的掛上去了。
  await screen.findByLabelText("送出");
  // agent 清單載入完成 → isAuthenticated 相關 effect 都跑過了。
  await waitFor(() => {
    expect(be.requestsFor("/v1/agents").length).toBeGreaterThan(0);
  });

  return { backend: be, ...utils };
}

/** 在 composer 打字並按送出。 */
export async function sendText(text) {
  const box = screen.getByPlaceholderText(/問 ANILA 任何事情/);
  await act(async () => {
    fireEvent.change(box, { target: { value: text } });
  });
  await act(async () => {
    fireEvent.click(screen.getByLabelText("送出"));
  });
}

/** 等到某一輪的回答文字出現在畫面上。 */
export async function waitForAnswer(text) {
  return screen.findByText(text, {}, { timeout: 3000 });
}

/** 等到所有串流結束(送出鈕回來、停止鈕消失)。 */
export async function waitForIdle() {
  await waitFor(() => {
    expect(screen.queryByLabelText("停止產生")).toBeNull();
  });
}

/**
 * 取得第 n 次(0-based)對話回合送出去的 messages 歷史,
 * 攤平成 `["user:…", "assistant:…"]` 好斷言。
 */
export function historyOf(backend, turnIndex) {
  const payload = backend.chatPayloads[turnIndex];
  if (!payload) return null;
  return payload.messages.map((m) => {
    const content =
      typeof m.content === "string"
        ? m.content
        : (m.content || [])
            .map((p) => (p.type === "text" ? p.text : `[${p.type}]`))
            .join("");
    return `${m.role}:${content}`;
  });
}

/**
 * 觸發「重新產生 → 重試（不調整）」。預設對最後一則助理訊息。
 * @param {number} [nth] 0-based;省略則取最後一則。
 */
export async function clickRegenerate(nth) {
  const triggers = screen.getAllByTitle("重新產生（可選調整方向）");
  const trigger = nth === undefined ? triggers.at(-1) : triggers[nth];
  await act(async () => {
    fireEvent.click(trigger);
  });
  const menu = await screen.findByRole("menu");
  await act(async () => {
    fireEvent.click(within(menu).getByText("重試（不調整）"));
  });
}

/**
 * 觸發使用者訊息的「編輯」→ 改寫 → 送出。
 * @param {number} [nth] 0-based;省略則取最後一則使用者訊息。
 */
export async function editUserMessage(nextText, nth) {
  const pencils = screen.getAllByTitle("編輯");
  const pencil = nth === undefined ? pencils.at(-1) : pencils[nth];
  await act(async () => {
    fireEvent.click(pencil);
  });
  // 編輯框是唯一一個「不是 composer」的 textarea(composer 靠 placeholder 認)。
  const editBox = await waitFor(() => {
    const box = [...document.querySelectorAll("textarea")].find(
      (t) => !/問 ANILA 任何事情/.test(t.placeholder || ""),
    );
    expect(box).toBeTruthy();
    return box;
  });
  await act(async () => {
    fireEvent.change(editBox, { target: { value: nextText } });
  });
  const submit = screen
    .getAllByText("送出")
    .find((el) => el.tagName === "BUTTON");
  await act(async () => {
    fireEvent.click(submit);
  });
}

/** 點側邊欄的某個對話(用標題定位)。 */
export async function selectConversation(title) {
  const label = await screen.findByTitle(title);
  const button = label.closest("button");
  await act(async () => {
    fireEvent.click(button);
  });
}

/** 開一個新對話(側邊欄的「新對話」)。 */
export async function clickNewChat() {
  const button = screen
    .getAllByText("新對話")
    .map((el) => el.closest("button"))
    .find(Boolean);
  await act(async () => {
    fireEvent.click(button);
  });
}

export { screen, waitFor, within, fireEvent, act, createFakeBackend };
