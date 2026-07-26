// W0-7 驗收④:子元件 throw → fallback 出現,且**輸出不含 stack 特徵**。
//
// 為什麼要斷言「不含 stack」而不只是「fallback 出現」:這條測試的真正目的是
// 釘住「涉密平台不對一般使用者洩漏內部路徑」這個設計決定。只測 fallback 出現
// 的話,未來有人為了方便排錯把 stack 加回畫面,測試依然是綠的。

import React from "react";
import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

import { ErrorBoundary, errorCode } from "../errorBoundary.jsx";

function Boom({ message = "kaboom" }) {
  throw new Error(message);
}

describe("errorCode", () => {
  it("是決定性的:同一輸入永遠同一代碼", () => {
    expect(errorCode("Error:kaboom")).toBe(errorCode("Error:kaboom"));
  });

  it("不同錯誤給不同代碼", () => {
    expect(errorCode("Error:a")).not.toBe(errorCode("Error:b"));
  });

  it("固定 6 碼、只含 base36 大寫字元", () => {
    for (const s of ["", "x", "Error:very long message ".repeat(10)]) {
      const code = errorCode(s);
      expect(code).toHaveLength(6);
      expect(code).toMatch(/^[0-9A-Z]{6}$/);
    }
  });
});

describe("ErrorBoundary", () => {
  let errorSpy;

  beforeEach(() => {
    // React 會自己 console.error 一次 render 錯誤;連同我們的兩行一起吞掉,
    // 但保留呼叫紀錄以便斷言我們真的有記 log。
    errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
  });

  afterEach(() => {
    errorSpy.mockRestore();
  });

  it("正常情況下直接渲染 children", () => {
    render(
      <ErrorBoundary>
        <p>ok</p>
      </ErrorBoundary>,
    );
    expect(screen.getByText("ok")).toBeInTheDocument();
  });

  it("子元件 throw 時渲染 fallback,而不是白畫面", () => {
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.getByText("這個畫面發生錯誤")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重新載入" })).toBeInTheDocument();
  });

  it("fallback 顯示與錯誤決定性對應的短代碼", () => {
    render(
      <ErrorBoundary>
        <Boom message="kaboom" />
      </ErrorBoundary>,
    );
    expect(screen.getByText(errorCode("Error:kaboom"))).toBeInTheDocument();
  });

  it("**不把 stack 或內部路徑渲染給使用者**(涉密要求)", () => {
    render(
      <ErrorBoundary>
        <Boom message="kaboom" />
      </ErrorBoundary>,
    );
    const shown = document.body.textContent || "";
    // stack 的特徵:"at " frame 標記、檔案路徑、副檔名
    expect(shown).not.toMatch(/\bat\s+\w+/);
    expect(shown).not.toMatch(/\.jsx?:\d+/);
    expect(shown).not.toContain("/src/");
    // 連原始 message 都不外顯 —— 它可能夾帶使用者內容或文件內文
    expect(shown).not.toContain("kaboom");
  });

  it("完整資訊有進 console(排錯不能因此變瞎)", () => {
    render(
      <ErrorBoundary>
        <Boom message="kaboom" />
      </ErrorBoundary>,
    );
    const logged = errorSpy.mock.calls.map((args) => String(args[0])).join("\n");
    expect(logged).toContain("[ANILA crash");
    expect(logged).toContain("component stack");
  });

  it("復原按鈕**不清 localStorage**(三個 SPA 同源,會誤傷別的 app)", () => {
    const clearSpy = vi.spyOn(window.localStorage, "clear");
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    );
    screen.getByRole("button", { name: "重新載入" }).click();
    expect(clearSpy).not.toHaveBeenCalled();
    clearSpy.mockRestore();
  });
});
