import React from "react";
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { ReasoningSummary } from "../chat.jsx";
import {
  THINKING_SUMMARY_PENDING,
  THINKING_SUMMARY_RAW_LABEL,
  formatThinkingComplete,
  formatThinkingAborted,
} from "../runtime/thinkingSummary.js";

afterEach(cleanup);

describe("ReasoningSummary 即時思考摘要", () => {
  it("思考中、尚無摘要：顯示正在思考與秒數，不輪播假步驟", () => {
    render(
      <ReasoningSummary
        trace={[]}
        reasoning={"The user asked about the universe. I should check language."}
        streaming
        thinkingStartedAt={Date.now() - 7000}
        thinkingSummaries={[]}
      />,
    );
    expect(screen.getByTestId("thinking-summary-headline").textContent).toBe(`${THINKING_SUMMARY_PENDING} 7秒`);
    expect(screen.getByTestId("thinking-elapsed").textContent).toMatch(/7秒/);
    expect(screen.getByTestId("thinking-summary-marquee")).toBeTruthy();
    expect(screen.queryByText(/The user asked/)).toBeNull();
    expect(screen.queryByText("分析問題")).toBeNull();
  });

  it("思考中：一行最新摘要；展開只見摘要歷程", () => {
    render(
      <ReasoningSummary
        trace={[]}
        reasoning={"raw dump that must not be the headline"}
        streaming
        thinkingStartedAt={Date.now() - 17000}
        thinkingSummaries={[
          { text: "探索宇宙奧秘的提問。", at: 1 },
          { text: "檢查語言偏好檔案是否影響回覆方式。", at: 2 },
        ]}
      />,
    );
    expect(screen.getByTestId("thinking-summary-headline").textContent).toBe("檢查語言偏好檔案是否影響回覆方式。 17秒");
    expect(screen.getByTestId("thinking-elapsed").textContent).toMatch(/17秒/);
    expect(screen.queryByTestId("thinking-summary-history")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /檢查語言偏好/ }));
    expect(screen.getByText("探索宇宙奧秘的提問。")).toBeTruthy();
    expect(screen.queryByText(/raw dump/)).toBeNull();
  });

  it("完成後收成已思考秒數，不標成失敗也不把原文當標題", () => {
    render(
      <ReasoningSummary
        trace={[]}
        reasoning={"full raw thinking"}
        streaming={false}
        thinkingStatus="complete"
        thinkingElapsedMs={31000}
        thinkingSummaries={[{ text: "規劃涵蓋暗物質等主題的簡潔回覆架構。", at: 1 }]}
        reasoningPersist={{ status: "full", original_chars: 16, kept_chars: 16 }}
      />,
    );
    expect(screen.getByText(formatThinkingComplete(31000))).toBeTruthy();
    expect(screen.queryByTestId("thinking-summary-marquee")).toBeNull();
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.queryByText("full raw thinking")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /已思考/ }));
    expect(screen.getByText("規劃涵蓋暗物質等主題的簡潔回覆架構。")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: THINKING_SUMMARY_RAW_LABEL }));
    expect(screen.getByText("full raw thinking")).toBeTruthy();
  });

  it("長度上限中止不得標成已思考完成", () => {
    render(
      <ReasoningSummary
        trace={[]}
        reasoning={"partial"}
        streaming={false}
        thinkingStatus="aborted"
        thinkingElapsedMs={12000}
        thinkingSummaries={[{ text: "探索宇宙奧秘的提問。", at: 1 }]}
      />,
    );
    expect(screen.getByText(formatThinkingAborted(12000))).toBeTruthy();
    expect(screen.queryByText(formatThinkingComplete(12000))).toBeNull();
  });

  it("摘要模式下仍顯示管理員鎖定與思考保存狀態", () => {
    render(
      <ReasoningSummary
        trace={[]}
        reasoning=""
        streaming={false}
        thinkingLocked
        thinkingStatus="complete"
        thinkingElapsedMs={31000}
        thinkingSummaries={[{ text: "規劃涵蓋暗物質等主題的簡潔回覆架構。", at: 1 }]}
        reasoningPersist={{
          status: "omitted",
          reason: "over_budget",
          original_chars: 20000,
          kept_chars: 0,
        }}
      />,
    );
    expect(screen.getByText(formatThinkingComplete(31000))).toBeTruthy();
    expect(screen.getByText("思考程度由管理員鎖定")).toBeTruthy();
    expect(screen.getByText("思考過長，未存入對話紀錄。")).toBeTruthy();
    expect(screen.queryByRole("button", { name: THINKING_SUMMARY_RAW_LABEL })).toBeNull();
  });

  it("思考球只出現在最新一則回覆", () => {
    const { rerender } = render(
      <ReasoningSummary
        trace={[]}
        reasoning={"raw"}
        streaming={false}
        thinkingStatus="complete"
        thinkingElapsedMs={13000}
        thinkingSummaries={[{ text: "探索宇宙奧秘的提問。", at: 1 }]}
        showThinkingOrb={false}
      />,
    );
    expect(screen.queryByRole("status")).toBeNull();
    rerender(
      <ReasoningSummary
        trace={[]}
        reasoning={"raw"}
        streaming={false}
        thinkingStatus="complete"
        thinkingElapsedMs={13000}
        thinkingSummaries={[{ text: "探索宇宙奧秘的提問。", at: 1 }]}
        showThinkingOrb
      />,
    );
    expect(screen.getByRole("status")).toBeTruthy();
  });
});
