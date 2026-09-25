// 串流中改為一行進度（摘要或最後一個真實步驟）；展開才看時間軸。
import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect } from "vitest";

import { ReasoningSummary } from "../chat.jsx";
import { THINKING_SUMMARY_PENDING } from "../runtime/thinkingSummary.js";

const t0 = 1_700_000_000_000;
const steps = [
  { kind: "thinking", label: "Router 分析意圖中", detail: "解析 query: 差勤規定", status: "ok", at: t0 },
  { kind: "retrieve", label: "檢索院內規章", detail: "軍人法規 · 8 筆命中", status: "ok", at: t0 + 800 },
  { kind: "thinking", label: "組合回答", detail: "", status: "running", at: t0 + 1300 },
];

describe("思考中的步驟時間軸", () => {
  it("串流中：一行顯示目前真實步驟，展開才看時間軸", () => {
    render(<ReasoningSummary trace={steps} reasoning={null} streaming stageLabel="組合回答" />);
    expect(screen.getByTestId("thinking-summary-headline").textContent).toMatch(/^組合回答/);
    expect(screen.queryByTestId("anila-steps")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /組合回答/ }));
    const list = screen.getByTestId("anila-steps");
    const items = list.querySelectorAll("[data-step]");
    expect(items.length).toBe(3);
    expect(screen.getByText("檢索院內規章")).toBeTruthy();
    expect(screen.getByText("軍人法規 · 8 筆命中")).toBeTruthy();
  });

  it("串流中還沒有任何步驟：顯示「正在思考」而不是空白", () => {
    render(<ReasoningSummary trace={[]} reasoning={null} streaming />);
    expect(screen.getByTestId("thinking-summary-headline").textContent).toMatch(new RegExp(`^${THINKING_SUMMARY_PENDING}`));
  });

  it("完成後：收成一行「N 步分析 · 用時 X 秒」，點開看得到每一步與各步耗時", () => {
    const done = steps.map((s) => ({ ...s, status: "ok" }));
    render(<ReasoningSummary trace={done} reasoning={null} streaming={false} finishedAt={t0 + 2100} />);
    const toggle = screen.getByRole("button", { name: /3 步分析/ });
    expect(toggle.textContent).toMatch(/用時 2\.1 秒/);
    expect(screen.queryByTestId("anila-steps")).toBeNull();
    fireEvent.click(toggle);
    const list = screen.getByTestId("anila-steps");
    expect(list.querySelectorAll("[data-step]").length).toBe(3);
    expect(list.textContent).toMatch(/0\.8 秒/);
    expect(list.textContent).toMatch(/0\.5 秒/);
  });

  it("沒有步驟也沒有思考文字、也不在串流：什麼都不畫", () => {
    const { container } = render(<ReasoningSummary trace={[]} reasoning={null} streaming={false} />);
    expect(container.firstChild).toBeNull();
  });
});

describe("瞬間完成的 Router 步驟", () => {
  const instant = [
    { kind: "thinking", label: "Router 分析意圖中", detail: "解析 query: 宇宙的歷史", status: "ok", at: t0 },
    { kind: "registry", label: "同步 agent 清單", detail: "已載入 0 個可用 agent", status: "ok", at: t0 },
  ];

  it("完成後不列出 0 秒的 Router 步驟，也不顯示 0.0 秒", () => {
    render(<ReasoningSummary trace={instant} reasoning={null} streaming={false} finishedAt={t0 + 13100} />);
    const toggle = screen.getByRole("button", { name: /1 步分析/ });
    expect(toggle.textContent).toMatch(/用時 13\.1 秒/);
    expect(toggle.textContent).not.toMatch(/2 步分析/);
    fireEvent.click(toggle);
    const list = screen.getByTestId("anila-steps");
    expect(list.querySelectorAll("[data-step]").length).toBe(1);
    expect(screen.getByText("思考中…")).toBeTruthy();
    expect(screen.queryByText("Router 分析意圖中")).toBeNull();
    expect(screen.queryByText(/已載入 0 個可用 agent/)).toBeNull();
    expect(list.textContent).toMatch(/13\.1 秒/);
    expect(list.textContent).not.toMatch(/0\.0 秒/);
  });

  it("搜尋過往對話即使很快也留在時間軸", () => {
    const trace = [
      { kind: "recall", label: "搜尋過往對話", detail: "", status: "ok", at: t0 },
      { kind: "direct", label: "Router 直接回答", detail: "", status: "ok", at: t0 + 10 },
    ];
    render(
      <ReasoningSummary trace={trace} reasoning={null} streaming={false} finishedAt={t0 + 5000} />,
    );
    fireEvent.click(screen.getByRole("button"));
    expect(screen.getByText("搜尋過往對話")).toBeTruthy();
  });

  it("串流中只留下進行中的思考步驟當一行進度", () => {
    render(<ReasoningSummary trace={instant} reasoning={null} streaming />);
    expect(screen.getByTestId("thinking-summary-headline").textContent).toMatch(/^思考中…/);
    expect(screen.queryByText("Router 分析意圖中")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /思考中/ }));
    const list = screen.getByTestId("anila-steps");
    const items = list.querySelectorAll("[data-step]");
    expect(items.length).toBe(1);
    expect(items[0].getAttribute("data-state")).toBe("done");
  });
});
