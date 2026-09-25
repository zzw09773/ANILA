import React from "react";
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { ReasoningSummary } from "../chat.jsx";
import { THINKING_SUMMARY_PENDING, THINKING_SUMMARY_RAW_LABEL } from "../runtime/thinkingSummary.js";
import { THINKING_STAGE_MARK } from "../runtime/thinkingStages.js";

afterEach(cleanup);

const started = Date.now() - 5200;

describe("思考階段清單的畫面", () => {
  it("沒有階段時仍是單一的思考中，不畫清單", () => {
    render(
      <ReasoningSummary
        trace={[]}
        reasoning="想了很久但沒有階段。"
        streaming
        thinkingStartedAt={started}
        thinkingSummaries={[]}
      />,
    );
    expect(screen.getByTestId("thinking-summary-headline").textContent)
      .toMatch(new RegExp(`^${THINKING_SUMMARY_PENDING}`));
    expect(screen.queryByTestId("thinking-stage-list")).toBeNull();
  });

  it("標題底下垂直列出階段：完成打勾、進行中有經過時間、失敗打叉", () => {
    render(
      <ReasoningSummary
        trace={[]}
        reasoning="原始思考內容在這裡。"
        streaming
        thinkingStartedAt={started}
        thinkingSummaries={[]}
        thinkingStages={[
          { index: 0, title: "拆解需求", status: "done", startedAt: started, endedAt: started + 1000 },
          { index: 1, title: "撰寫第一章", status: "running", startedAt: Date.now() - 4000 },
          { index: 2, title: "核對出處", status: "error", startedAt: started, endedAt: started + 2000 },
        ]}
      />,
    );
    expect(screen.getByTestId("thinking-summary-headline").textContent)
      .toMatch(new RegExp(`^${THINKING_SUMMARY_PENDING}`));
    const list = screen.getByTestId("thinking-stage-list");
    const items = [...list.querySelectorAll("[data-testid='thinking-stage']")];
    expect(items.map((item) => item.getAttribute("data-status"))).toEqual([
      "done",
      "running",
      "error",
    ]);
    expect(items[0].textContent).toContain(THINKING_STAGE_MARK.done);
    expect(items[0].textContent).toContain("拆解需求");
    expect(items[1].textContent).toContain(THINKING_STAGE_MARK.running);
    expect(items[1].textContent).toContain("撰寫第一章");
    expect(items[1].textContent).toMatch(/4秒/);
    expect(items[2].textContent).toContain(THINKING_STAGE_MARK.error);
    expect(items[2].textContent).toContain("核對出處");
    expect(screen.queryByText("原始思考內容在這裡。")).toBeNull();
  });

  it("展開仍看得到原始思考；停止的階段用方塊", () => {
    render(
      <ReasoningSummary
        trace={[]}
        reasoning="原始思考內容在這裡。"
        streaming={false}
        thinkingStatus="complete"
        thinkingElapsedMs={12000}
        thinkingSummaries={[]}
        thinkingStages={[
          { index: 0, title: "拆解需求", status: "done" },
          { index: 1, title: "撰寫第一章", status: "stopped" },
        ]}
      />,
    );
    expect(screen.getByText("拆解需求")).toBeTruthy();
    expect(screen.getByText("撰寫第一章")).toBeTruthy();
    expect(screen.getByTestId("thinking-stage-list").textContent).toContain(THINKING_STAGE_MARK.stopped);
    const raw = screen.getByTestId("raw-reasoning");
    expect(raw.hidden).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: THINKING_SUMMARY_RAW_LABEL }));
    expect(raw.hidden).toBe(false);
    expect(raw.textContent).toContain("原始思考內容在這裡。");
  });
});
