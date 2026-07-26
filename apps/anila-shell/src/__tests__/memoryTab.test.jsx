// 設定 → 記憶 tab 依部署能力分流(W1-3 ②)。
//
// 缺陷:`ENABLE_MEMORY` 預設 False(config.py:38,card 分支明確 false),而
// 空狀態文案寫「和 ANILA 多聊聊…平台會自動學習」——功能沒開,這句是假的。
//
// 這裡釘死三件事:
//   1. 關閉時:出現「本部署未啟用記憶功能」,且**不含**任何「會自動學習 /
//      會自動帶入」的承諾字樣。
//   2. 關閉時仍保留既有殘留資料的檢視與刪除路徑(刪除權不能被旗標吃掉)。
//   3. 開啟時:行為與現況一致(承諾字樣回來)。

import React from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

import { MemoryTab } from "../memoryTab.jsx";
import { ConfirmProvider } from "../confirm.jsx";

function renderTab({ enableMemory, facts = [], chunks = [] }) {
  const request = vi.fn(async (path) => {
    if (path.startsWith("/api/memory/facts")) {
      return { facts, total: facts.length };
    }
    if (path.startsWith("/api/memory/chunks")) {
      return {
        items: chunks,
        total: chunks.length,
        encrypted_total: 0,
        distinct_conversations: 0,
      };
    }
    throw new Error(`unexpected path ${path}`);
  });
  render(
    <ConfirmProvider>
      <MemoryTab
        authRequest={request}
        capabilities={{ enableMemory, enablePublicShare: false }}
      />
    </ConfirmProvider>,
  );
  return request;
}

describe("MemoryTab — enable_memory=false", () => {
  it("says the deployment has memory turned off", async () => {
    renderTab({ enableMemory: false });
    expect(
      await screen.findByText(/本部署未啟用記憶功能/),
    ).toBeInTheDocument();
  });

  it("never promises that the platform will learn on its own", async () => {
    renderTab({ enableMemory: false });
    await screen.findByText(/本部署未啟用記憶功能/);
    const body = document.body.textContent || "";
    expect(body).not.toMatch(/自動學習/);
    expect(body).not.toMatch(/自動帶入/);
    expect(body).not.toMatch(/會在每輪對話後/);
  });

  it("still lets the user inspect and clear residual data (deletion right)", async () => {
    renderTab({
      enableMemory: false,
      facts: [{ id: 1, key: "暱稱", value: "小王", confidence: 0.9 }],
    });
    expect(await screen.findByText("暱稱")).toBeInTheDocument();
    const clearButtons = await screen.findAllByRole("button", { name: "清空全部" });
    expect(clearButtons.length).toBeGreaterThanOrEqual(1);
    expect(clearButtons[0]).toBeEnabled();
  });
});

describe("MemoryTab — enable_memory=true", () => {
  it("keeps today's behaviour verbatim", async () => {
    renderTab({ enableMemory: true });
    expect(await screen.findByText(/自動學習/)).toBeInTheDocument();
    expect(screen.getByText(/會在每輪對話後/)).toBeInTheDocument();
    expect(screen.queryByText(/本部署未啟用記憶功能/)).toBeNull();
  });

  it("loads both memory endpoints", async () => {
    const request = renderTab({ enableMemory: true });
    await waitFor(() => expect(request).toHaveBeenCalledTimes(2));
  });
});

describe("MemoryTab — capability prop missing", () => {
  it("fails closed rather than promising a feature", async () => {
    const request = vi.fn(async (path) =>
      path.startsWith("/api/memory/facts")
        ? { facts: [], total: 0 }
        : { items: [], total: 0, encrypted_total: 0, distinct_conversations: 0 },
    );
    render(
      <ConfirmProvider>
        <MemoryTab authRequest={request} />
      </ConfirmProvider>,
    );
    expect(await screen.findByText(/本部署未啟用記憶功能/)).toBeInTheDocument();
  });
});
