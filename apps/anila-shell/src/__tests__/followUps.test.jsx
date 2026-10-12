import { describe, it, expect, afterEach, vi } from "vitest";
import React from "react";
import { render, screen, fireEvent, act, cleanup } from "@testing-library/react";

import { normalizeFollowUps, fetchFollowUps, followUpsPath, followUpsEnabledFrom } from "../runtime/followUps.js";
import { NextQuestionChips, FollowUpSuggestions } from "../trust.jsx";
import { Composer } from "../chat.jsx";
import { ConfirmProvider } from "../confirm.jsx";
import {
  mountOrchestrator,
  createFakeBackend,
  sendText,
  waitForIdle,
  composerBox,
  waitFor,
  selectConversation,
} from "./helpers/orchestrator.jsx";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  sessionStorage.clear();
});

describe("followUps runtime", () => {
  it("normalize：去空白、去重、丟非字串與過長，最多 3 個", () => {
    expect(normalizeFollowUps(["  a  b ", "a b", 3, "", "x".repeat(81), "c", "d", "e"]))
      .toEqual(["a b", "c", "d"]);
    expect(normalizeFollowUps(null)).toEqual([]);
  });

  it("path 與 opt-out 預設", () => {
    expect(followUpsPath(7, 42)).toBe("/api/conversations/7/messages/42/follow-ups");
    expect(followUpsEnabledFrom({})).toBe(true);
    expect(followUpsEnabledFrom({ followUpsEnabled: false })).toBe(false);
  });

  it("fetch：POST 到端點；任何失敗都是 []，不丟錯", async () => {
    const ok = vi.fn().mockResolvedValue({ suggestions: ["下一題？"] });
    expect(await fetchFollowUps(ok, 1, 2)).toEqual(["下一題？"]);
    expect(ok.mock.calls[0][0]).toBe("/api/conversations/1/messages/2/follow-ups");
    expect(ok.mock.calls[0][1].method).toBe("POST");
    const boom = vi.fn().mockRejectedValue(new Error("502"));
    expect(await fetchFollowUps(boom, 1, 2)).toEqual([]);
    const weird = vi.fn().mockResolvedValue({ nope: true });
    expect(await fetchFollowUps(weird, 1, 2)).toEqual([]);
    expect(await fetchFollowUps(ok, null, 2)).toEqual([]);
  });

  it("fetch：abort 之後就算回來也是 []", async () => {
    const controller = new AbortController();
    const slow = vi.fn(() => new Promise((resolve) => setTimeout(() => resolve({ suggestions: ["晚到"] }), 5)));
    const p = fetchFollowUps(slow, 1, 2, controller.signal);
    controller.abort();
    expect(await p).toEqual([]);
  });
});

describe("NextQuestionChips", () => {
  it("一般信心也顯示，點了呼叫 onPick，有 aria-live 通知", () => {
    const onPick = vi.fn();
    render(<FollowUpSuggestions suggestions={["那預算呢？", "要多久？"]} confidence={{ level: "high" }} onPick={onPick} />);
    expect(screen.getByText("你可能還想問")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "帶入輸入框：要多久？" }));
    expect(onPick).toHaveBeenCalledWith("要多久？");
    expect(screen.getByText("有 2 個建議問題").getAttribute("aria-live")).toBe("polite");
  });

  it("低信心多一行說明；空清單不渲染", () => {
    const { container, rerender } = render(<FollowUpSuggestions suggestions={["再確認一次？"]} confidence={{ level: "low" }} />);
    expect(screen.getByText(/信心偏低/)).toBeTruthy();
    rerender(<NextQuestionChips suggestions={[]} />);
    expect(container.firstChild).toBeNull();
  });
});

function renderComposer(props = {}) {
  const onSend = vi.fn(() => true);
  const utils = render(
    <ConfirmProvider>
      <Composer onSend={onSend} agents={[]} {...props} />
    </ConfirmProvider>,
  );
  const box = screen.getByLabelText("傳訊息給 ANILA");
  return { ...utils, onSend, box };
}

describe("Composer 灰字預覽與帶入", () => {
  it("空的時候顯示第一個建議，Tab 採用", () => {
    const { box } = renderComposer({ ghostSuggestion: "那預算呢？" });
    expect(box.getAttribute("placeholder")).toBe("那預算呢？");
    expect(screen.getByTestId("composer-ghost-hint")).toBeTruthy();
    const ev = fireEvent.keyDown(box, { key: "Tab" });
    expect(ev).toBe(false); // preventDefault
    expect(box.value).toBe("那預算呢？");
    expect(screen.queryByTestId("composer-ghost-hint")).toBeNull();
  });

  it("有字時 Tab 不攔截", () => {
    const { box } = renderComposer({ ghostSuggestion: "那預算呢？" });
    fireEvent.change(box, { target: { value: "我自己的" } });
    const ev = fireEvent.keyDown(box, { key: "Tab" });
    expect(ev).toBe(true);
    expect(box.value).toBe("我自己的");
  });

  it("Esc 收起預覽；Shift+Tab 不攔截", () => {
    const { box } = renderComposer({ ghostSuggestion: "那預算呢？" });
    expect(fireEvent.keyDown(box, { key: "Tab", shiftKey: true })).toBe(true);
    fireEvent.keyDown(box, { key: "Escape" });
    expect(box.getAttribute("placeholder")).not.toBe("那預算呢？");
    expect(fireEvent.keyDown(box, { key: "Tab" })).toBe(true);
  });

  it("insertRequest 帶進輸入框，不送出；打字會通知 onTyping", async () => {
    const onTyping = vi.fn();
    const { box, onSend, rerender } = renderComposer({ onTyping });
    await act(async () => {
      rerender(
        <ConfirmProvider>
          <Composer onSend={onSend} agents={[]} onTyping={onTyping} insertRequest={{ text: "接著問這個", token: 1 }} />
        </ConfirmProvider>,
      );
    });
    expect(box.value).toBe("接著問這個");
    expect(onSend).not.toHaveBeenCalled();
    fireEvent.change(box, { target: { value: "接著問這個吧" } });
    expect(onTyping).toHaveBeenCalled();
  });
});

describe("建議問題：整條資料流", () => {
  const followUpCalls = (backend) => backend.requestsFor(/\/messages\/\d+\/follow-ups$/, "POST");

  it("串流中不呼叫；完成後呼叫一次，chip 只掛在最後一則，點了只帶入", async () => {
    const backend = createFakeBackend();
    const served = [];
    backend.route("POST", (p) => /\/follow-ups$/.test(p), (req, { jsonResponse }) => {
      served.push(req.path);
      return jsonResponse({ suggestions: served.length === 1 ? ["第一輪追問？"] : ["第二輪追問？"] });
    });
    await mountOrchestrator({ backend });
    backend.enqueueAnswer("第一個回答，內容足夠長");
    await sendText("第一題");
    await waitForIdle();
    await screen.findByRole("button", { name: "帶入輸入框：第一輪追問？" });
    expect(followUpCalls(backend)).toHaveLength(1);
    // 灰字預覽
    expect(composerBox().getAttribute("placeholder")).toBe("第一輪追問？");

    backend.enqueueAnswer("第二個回答");
    await sendText("第二題");
    await waitForIdle();
    await screen.findByRole("button", { name: "帶入輸入框：第二輪追問？" });
    expect(screen.queryByRole("button", { name: "帶入輸入框：第一輪追問？" })).toBeNull();
    expect(followUpCalls(backend)).toHaveLength(2);

    const before = backend.chatPayloads.length;
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "帶入輸入框：第二輪追問？" }));
    });
    await waitFor(() => expect(composerBox().value).toBe("第二輪追問？"));
    expect(backend.chatPayloads.length).toBe(before);
  });

  it("失敗時靜默：沒有 chip、沒有錯誤提示", async () => {
    const backend = createFakeBackend();
    backend.route("POST", (p) => /\/follow-ups$/.test(p), (req, { errorResponse }) => errorResponse(502, "上游壞了"));
    await mountOrchestrator({ backend });
    backend.enqueueAnswer("回答");
    await sendText("問題");
    await waitForIdle();
    await waitFor(() => expect(followUpCalls(backend)).toHaveLength(1));
    expect(screen.queryByTestId("next-question-chips")).toBeNull();
    expect(screen.queryByText(/上游壞了/)).toBeNull();
  });

  it("opt-out：使用者關掉就不呼叫", async () => {
    const backend = createFakeBackend({ uiSettings: { followUpsEnabled: false } });
    await mountOrchestrator({ backend });
    backend.enqueueAnswer("回答");
    await sendText("問題");
    await waitForIdle();
    await new Promise((r) => setTimeout(r, 30));
    expect(followUpCalls(backend)).toHaveLength(0);
  });

  it("開始打字就 abort 還在跑的請求", async () => {
    const backend = createFakeBackend();
    let seen = null;
    backend.route("POST", (p) => /\/follow-ups$/.test(p), (req, { jsonResponse }) => {
      seen = req.init.signal;
      return new Promise((resolve) => setTimeout(() => resolve(jsonResponse({ suggestions: ["太晚了？"] })), 80));
    });
    await mountOrchestrator({ backend });
    backend.enqueueAnswer("回答");
    await sendText("問題");
    await waitForIdle();
    await waitFor(() => expect(seen).not.toBeNull());
    await act(async () => {
      fireEvent.change(composerBox(), { target: { value: "我" } });
    });
    expect(seen.aborted).toBe(true);
    await new Promise((r) => setTimeout(r, 120));
    expect(screen.queryByRole("button", { name: "帶入輸入框：太晚了？" })).toBeNull();
  });

  it("重載對話時從 meta 讀回，不重算", async () => {
    const backend = createFakeBackend({
      conversations: [
        { id: 55, title: "既有對話甲", agent_id: null, classified: false, tags: [], starred: false, folder: "all" },
      ],
    });
    backend.disableTitleGeneration();
    backend.route("GET", /^\/api\/conversations\/55$/, (_r, { jsonResponse }) =>
      jsonResponse({
        id: 55,
        title: "既有對話甲",
        active_leaf_message_id: 2,
        messages: [
          { id: 1, role: "user", content: "上次問的", parent_id: null, sibling_index: 0, sibling_count: 1, sibling_ids: [1] },
          { id: 2, role: "assistant", content: "上次答的", parent_id: 1, sibling_index: 0, sibling_count: 1, sibling_ids: [2], metadata: { follow_ups: ["meta 裡的追問？"] } },
        ],
      }),
    );
    await mountOrchestrator({ backend });
    await selectConversation("既有對話甲");
    await screen.findByRole("button", { name: "帶入輸入框：meta 裡的追問？" });
    expect(followUpCalls(backend)).toHaveLength(0);
  });
});

import { SettingsModal } from "../components/SettingsModal.jsx";

describe("設定：回答後顯示建議問題", () => {
  it("一般分頁有開關，按了回報 on/off", () => {
    const onChange = vi.fn();
    render(
      <ConfirmProvider>
        <SettingsModal open tab="general" setTab={() => {}} onClose={() => {}} user={{}} agents={[]}
          authRequest={vi.fn()} followUpsEnabled onFollowUpsEnabledChange={onChange} />
      </ConfirmProvider>,
    );
    const on = screen.getByRole("button", { name: "顯示建議問題" });
    expect(on.getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(screen.getByRole("button", { name: "不要顯示" }));
    expect(onChange).toHaveBeenCalledWith(false);
  });
});
