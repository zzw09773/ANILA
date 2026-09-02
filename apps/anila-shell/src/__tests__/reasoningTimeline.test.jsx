// 「送出後 AI 思考中、每一步的狀態」的畫面（擁有者 2026-09-02：可不可以做得更漂亮）。
// 以前串流中只有一顆小轉圈＋「組合回答…」一行字；做完只剩「1 步分析」。
// 新形狀：
//   串流中 —— 已收到的步驟以時間軸列出：完成的打勾、進行中的那一步有活動指示與 aria-current；
//   完成後 —— 收成一行「N 步分析 · 用時 X 秒」，可展開看每一步（含各步耗時）。
import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect } from "vitest";

import { ReasoningSummary } from "../chat.jsx";

const t0 = 1_700_000_000_000;
const steps = [
  { kind: "thinking", label: "Router 分析意圖中", detail: "解析 query: 差勤規定", status: "ok", at: t0 },
  { kind: "retrieve", label: "檢索院內規章", detail: "軍人法規 · 8 筆命中", status: "ok", at: t0 + 800 },
  { kind: "thinking", label: "組合回答", detail: "", status: "running", at: t0 + 1300 },
];

describe("思考中的步驟時間軸", () => {
  it("串流中：列出已收到的每一步，最後一步是進行中（aria-current=step），前面的標記為完成", () => {
    render(<ReasoningSummary trace={steps} reasoning={null} streaming stageLabel="組合回答" />);
    const list = screen.getByTestId("anila-steps");
    const items = list.querySelectorAll("[data-step]");
    expect(items.length).toBe(3);
    expect(items[0].getAttribute("data-state")).toBe("done");
    expect(items[1].getAttribute("data-state")).toBe("done");
    expect(items[2].getAttribute("data-state")).toBe("active");
    expect(items[2].getAttribute("aria-current")).toBe("step");
    expect(screen.getByText("檢索院內規章")).toBeTruthy();
    expect(screen.getByText("軍人法規 · 8 筆命中")).toBeTruthy();
    // 串流中的狀態要有一個 live region，螢幕閱讀器才知道在進行
    expect(list.closest("[aria-live], [role=status]") || list.getAttribute("aria-live")).toBeTruthy();
  });

  it("串流中還沒有任何步驟：顯示「思考中」而不是空白", () => {
    render(<ReasoningSummary trace={[]} reasoning={null} streaming />);
    expect(screen.getByText(/思考中/)).toBeTruthy();
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
    // 各步耗時：第 1 步 0.8 秒、第 2 步 0.5 秒
    expect(list.textContent).toMatch(/0\.8 秒/);
    expect(list.textContent).toMatch(/0\.5 秒/);
  });

  it("沒有步驟也沒有思考文字、也不在串流：什麼都不畫", () => {
    const { container } = render(<ReasoningSummary trace={[]} reasoning={null} streaming={false} />);
    expect(container.firstChild).toBeNull();
  });
});
