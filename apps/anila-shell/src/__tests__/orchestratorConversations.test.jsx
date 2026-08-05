// 不變式:「一個對話的內容不會滲到另一個對話」。
//
// 訊息以 `messagesByConv[convId]` 分格保存,送出時取的歷史也是從那一格取。
// 只要取錯格、或是切換時忘了跟著 selectedConvId 走,使用者會在 B 對話裡
// 看到 A 對話的內容 —— 在這個平台上,那不只是難看,是列管資料跨對話外流。

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

import {
  mountOrchestrator,
  sendText,
  waitForAnswer,
  waitForIdle,
  selectConversation,
  clickNewChat,
  historyOf,
  createFakeBackend,
  screen,
  waitFor,
} from "./helpers/orchestrator.jsx";

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("orchestrator — 切換對話不會互相污染", () => {
  it("在新對話裡送出時,歷史不含前一個對話的內容", async () => {
    const backend = createFakeBackend();
    backend.disableTitleGeneration();
    backend.enqueueAnswer("甲的回答").enqueueAnswer("乙的回答");
    await mountOrchestrator({ backend });

    await sendText("甲對話的問題");
    await waitForAnswer("甲的回答");
    await waitForIdle();

    await clickNewChat();
    await sendText("乙對話的問題");
    await waitForAnswer("乙的回答");
    await waitForIdle();

    // 第二輪的歷史只能有乙自己那一句。
    expect(historyOf(backend, 1)).toEqual(["user:乙對話的問題"]);
    // 而且真的建了第二個對話,不是把兩輪塞進同一個。
    expect(
      backend.requestsFor(/^\/api\/conversations$/, "POST"),
    ).toHaveLength(2);
  });

  it("切到乙對話時,甲對話的訊息不在畫面上;切回來又在", async () => {
    const backend = createFakeBackend();
    backend.disableTitleGeneration();
    backend.enqueueAnswer("甲的回答").enqueueAnswer("乙的回答");
    await mountOrchestrator({ backend });

    await sendText("甲對話的問題");
    await waitForAnswer("甲的回答");
    await waitForIdle();

    await clickNewChat();
    await sendText("乙對話的問題");
    await waitForAnswer("乙的回答");
    await waitForIdle();

    // 現在停在乙:甲的內容必須完全看不到。
    await waitFor(() => {
      expect(screen.queryByText("甲的回答")).toBeNull();
    });
    expect(screen.getByText("乙的回答")).toBeInTheDocument();

    // 切回甲:甲的內容回來,乙的不見。
    await selectConversation("甲對話的問題");
    await waitFor(() => {
      expect(screen.getByText("甲的回答")).toBeInTheDocument();
    });
    expect(screen.queryByText("乙的回答")).toBeNull();
  });

  it("切回甲之後再送一輪,歷史接的是甲的上下文", async () => {
    const backend = createFakeBackend();
    backend.disableTitleGeneration();
    backend
      .enqueueAnswer("甲的回答")
      .enqueueAnswer("乙的回答")
      .enqueueAnswer("甲的第二個回答");
    await mountOrchestrator({ backend });

    await sendText("甲對話的問題");
    await waitForAnswer("甲的回答");
    await waitForIdle();

    await clickNewChat();
    await sendText("乙對話的問題");
    await waitForAnswer("乙的回答");
    await waitForIdle();

    await selectConversation("甲對話的問題");
    await waitFor(() => {
      expect(screen.getByText("甲的回答")).toBeInTheDocument();
    });

    await sendText("甲的追問");
    await waitForAnswer("甲的第二個回答");
    await waitForIdle();

    expect(historyOf(backend, 2)).toEqual([
      "user:甲對話的問題",
      "assistant:甲的回答",
      "user:甲的追問",
    ]);
    // 乙的字一個都不准出現在甲的歷史裡。
    expect(historyOf(backend, 2).join("|")).not.toMatch(/乙/);
  });

  it("點開伺服器上既有的對話,載入的是那個對話自己的訊息", async () => {
    const backend = createFakeBackend({
      conversations: [
        { id: 55, title: "既有對話甲", agent_id: null, classified: false, tags: [], starred: false, folder: "all" },
        { id: 66, title: "既有對話乙", agent_id: null, classified: false, tags: [], starred: false, folder: "all" },
      ],
    });
    backend.disableTitleGeneration();
    // 在假後端裡先種好兩個對話各自的訊息。
    backend.route("GET", /^\/api\/conversations\/55$/, (_r, { jsonResponse }) =>
      jsonResponse({
        id: 55,
        title: "既有對話甲",
        active_leaf_message_id: 2,
        messages: [
          { id: 1, role: "user", content: "甲的舊問題", parent_id: null, sibling_index: 0, sibling_count: 1, sibling_ids: [1] },
          { id: 2, role: "assistant", content: "甲的舊回答", parent_id: 1, sibling_index: 0, sibling_count: 1, sibling_ids: [2] },
        ],
      }),
    );
    backend.route("GET", /^\/api\/conversations\/66$/, (_r, { jsonResponse }) =>
      jsonResponse({
        id: 66,
        title: "既有對話乙",
        active_leaf_message_id: 4,
        messages: [
          { id: 3, role: "user", content: "乙的舊問題", parent_id: null, sibling_index: 0, sibling_count: 1, sibling_ids: [3] },
          { id: 4, role: "assistant", content: "乙的舊回答", parent_id: 3, sibling_index: 0, sibling_count: 1, sibling_ids: [4] },
        ],
      }),
    );

    await mountOrchestrator({ backend });

    await selectConversation("既有對話甲");
    await waitFor(() => {
      expect(screen.getByText("甲的舊回答")).toBeInTheDocument();
    });
    expect(screen.queryByText("乙的舊回答")).toBeNull();

    await selectConversation("既有對話乙");
    await waitFor(() => {
      expect(screen.getByText("乙的舊回答")).toBeInTheDocument();
    });
    expect(screen.queryByText("甲的舊回答")).toBeNull();
  });

  it("接續既有對話送出時,歷史帶的是伺服器上載回來的訊息", async () => {
    const backend = createFakeBackend({
      conversations: [
        { id: 55, title: "既有對話甲", agent_id: null, classified: false, tags: [], starred: false, folder: "all" },
      ],
    });
    backend.disableTitleGeneration();
    backend.enqueueAnswer("接續的回答");
    backend.route("GET", /^\/api\/conversations\/55$/, (_r, { jsonResponse }) =>
      jsonResponse({
        id: 55,
        title: "既有對話甲",
        active_leaf_message_id: 2,
        messages: [
          { id: 1, role: "user", content: "上次問的", parent_id: null, sibling_index: 0, sibling_count: 1, sibling_ids: [1] },
          { id: 2, role: "assistant", content: "上次答的", parent_id: 1, sibling_index: 0, sibling_count: 1, sibling_ids: [2] },
        ],
      }),
    );

    await mountOrchestrator({ backend });
    await selectConversation("既有對話甲");
    await waitFor(() => {
      expect(screen.getByText("上次答的")).toBeInTheDocument();
    });

    await sendText("這次問的");
    await waitForAnswer("接續的回答");

    // 重新整理後接著問,模型必須看得到上一次的內容 —— 這正是
    // 「每一輪都送空歷史」那個缺陷最傷使用者的地方。
    expect(historyOf(backend, 0)).toEqual([
      "user:上次問的",
      "assistant:上次答的",
      "user:這次問的",
    ]);
  });
});
