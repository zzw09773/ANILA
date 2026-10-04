// 新對話第一次送出若在對話建立之後失敗，原文要回到這個新對話。
// 比較模式未登入要讓 composer 留住原文；串流失敗則留在錯誤列。

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

const TWO_AGENTS = [
  {
    id: "demo-agent",
    name: "示範助手",
    short: "demo",
    description: "測試用助手",
    endpoint_url: "https://example.invalid/v1",
    capabilities: {},
    requires_encryption: false,
  },
  {
    id: "second-agent",
    name: "第二助手",
    short: "second",
    description: "比較模式需要兩個",
    endpoint_url: "https://example.invalid/v1",
    capabilities: {},
    requires_encryption: false,
  },
];

beforeEach(() => {
  window.localStorage.clear();
  window.sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

async function enterCompare() {
  const compareBtn = await waitFor(() => {
    const el = [...document.querySelectorAll("[title^='比較模式']")].find(
      (node) => !/需至少/.test(node.getAttribute("title") || ""),
    );
    expect(el).toBeTruthy();
    return el;
  });
  await act(async () => {
    fireEvent.click(compareBtn);
  });
}

describe("新對話首次送出失敗", () => {
  it("對話已經被這次送出建立並選取後，失敗仍把原文放回輸入框", async () => {
    const backend = createFakeBackend({ conversations: [] });
    backend.disableTitleGeneration();
    backend.deferTurnPersist = true;
    await mountOrchestrator({ backend });

    await sendText("原始內容");
    await waitFor(() => {
      expect(screen.getAllByTitle("原始內容").length).toBeGreaterThan(0);
      expect(backend.requestsFor("/turn", "POST").length).toBeGreaterThan(0);
    });
    expect(composerBox().value).toBe("");

    await act(async () => {
      backend.resolveTurnPersist({ ok: false, status: 500, error: "這一輪沒有順利送出" });
    });
    await waitFor(() => {
      expect(composerBox().value).toBe("原始內容");
    });
    expect(backend.chatPayloads).toHaveLength(0);
  });
});

describe("比較模式送出", () => {
  it("未登入時不把輸入清成成功送出", async () => {
    const backend = createFakeBackend({ agents: TWO_AGENTS });
    backend.disableTitleGeneration();
    await mountOrchestrator({ backend });
    await enterCompare();

    fireEvent.click(screen.getByText("tester"));
    const logout = await screen.findByText("登出");
    await act(async () => {
      fireEvent.click(logout);
    });
    await waitFor(() => {
      expect(screen.queryByText("tester")).toBeNull();
    });

    const box = composerBox();
    fireEvent.change(box, { target: { value: "比較這題" } });
    await act(async () => {
      fireEvent.click(screen.getByLabelText("送出"));
    });

    expect(composerBox().value).toBe("比較這題");
    expect(screen.getByRole("alert").textContent).toContain("尚未登入");
    expect(backend.chatPayloads).toHaveLength(0);
    expect(screen.queryByTestId("message-stream-error")).toBeNull();
  });

  it("比較列已經建立後，串流錯誤留在錯誤列，不把原文塞回輸入框", async () => {
    const backend = createFakeBackend({ agents: TWO_AGENTS });
    backend.disableTitleGeneration();
    backend.enqueueHttpError(500, "比較產生失敗").enqueueHttpError(500, "比較產生失敗");
    await mountOrchestrator({ backend });
    await enterCompare();
    await sendText("比較這題");
    await waitForIdle();

    expect(composerBox().value).toBe("");
    expect(screen.getAllByTestId("message-stream-error").some(
      (node) => /比較產生失敗/.test(node.textContent),
    )).toBe(true);
    expect(screen.getAllByText("比較這題").length).toBeGreaterThan(0);
  });
});

describe("品牌回到首頁", () => {
  it("打到一半回首頁再進原對話，250ms 內原文還在且沒有送出，並關掉用量與專案入口", async () => {
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
          { id: 1, role: "user", content: "甲的舊問題", parent_id: null, sibling_index: 0, sibling_count: 1, sibling_ids: [1] },
          { id: 2, role: "assistant", content: "甲的舊回答", parent_id: 1, sibling_index: 0, sibling_count: 1, sibling_ids: [2] },
        ],
      }),
    );
    await mountOrchestrator({ backend });

    const row = await screen.findByTitle("既有對話甲");
    await act(async () => {
      fireEvent.click(row.closest("button"));
    });
    await waitFor(() => {
      expect(screen.getByText("甲的舊回答")).toBeTruthy();
    });

    const box = composerBox();
    fireEvent.change(box, { target: { value: "寫到一半先回首頁" } });
    expect(window.sessionStorage.getItem("anila-draft:55")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "平台入口" }));
    fireEvent.click(await screen.findByText("用量"));
    await screen.findByRole("dialog", { name: "我的用量" });

    const home = screen.getByRole("button", { name: "回到首頁" });
    expect(home).not.toBe(screen.getByRole("button", { name: "收合側邊" }));
    await act(async () => {
      fireEvent.click(home);
    });

    expect(composerBox().value).toBe("寫到一半先回首頁");
    expect(screen.queryByText("甲的舊回答")).toBeNull();
    expect(screen.queryByRole("dialog", { name: "我的用量" })).toBeNull();
    expect(screen.getByText("你今天想問 ANILA 什麼？")).toBeTruthy();
    expect(window.sessionStorage.getItem("anila-draft:55")).toBe("寫到一半先回首頁");
    expect(backend.chatPayloads).toHaveLength(0);

    await act(async () => {
      fireEvent.click(screen.getByTitle("既有對話甲").closest("button"));
    });
    await waitFor(() => {
      expect(composerBox().value).toBe("寫到一半先回首頁");
    });
    expect(screen.getByText("甲的舊回答")).toBeTruthy();
    expect(backend.chatPayloads).toHaveLength(0);
  });
});
