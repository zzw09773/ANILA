// 上線前端的錯誤網子（finding-no-error-boundary-20260820，HIGH）。
//
// render 期例外以前＝React 卸載整棵樹＝白畫面、零訊息——而「白畫面不會報錯」
// 是本專案付過學費的形狀。這裡照工單驗收寫：
//   ① 同一次渲染：正向錨點（可讀訊息真的渲染出來）＋負向斷言（同一個節點不含
//      stack／絕對路徑／內部主機名）——不拆兩條測試；
//   ② 會拋的元件真的拋，文字由 boundary 產生，測試自己不 render 那段文字；
//   ③ 關聯碼只有在 grep 得到伺服器 log 時才放；做不到就只放時間戳（本 app 沒有
//      把前端錯誤送到伺服器的通道，所以是時間戳）。
// 突變：拿掉 fallback 渲染那行 → ① 必紅（登記在 scripts/mutation-check.mjs）。

import React from "react";
import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

import { ErrorBoundary } from "../ErrorBoundary.jsx";

function Boom() {
  // 真的在 render 期拋——不是 effect、不是 event handler。
  throw new Error("Element type is invalid at /srv/anila/apps/anila-shell/src/services.jsx on host csp-internal.local");
}

let consoleError;
beforeEach(() => {
  // React 與 boundary 自己都會 console.error；壓掉雜訊但保留可斷言。
  consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
});
afterEach(() => {
  consoleError.mockRestore();
});

describe("ErrorBoundary — render 期例外變成可讀的錯誤區塊", () => {
  it("同一個節點：有可讀訊息＋時間戳，沒有 stack／路徑／主機名（①②③）", () => {
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    );
    const panel = screen.getByRole("alert");
    const text = panel.textContent;
    // ① 正向錨點：訊息是 boundary 產生的，測試沒有 render 這段字。
    expect(text).toContain("系統發生錯誤");
    expect(text).toMatch(/重新整理|聯繫維運/);
    // ③ 時間戳（ISO 形狀），沒有假的關聯碼。
    expect(text).toMatch(/\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/);
    expect(text).not.toMatch(/關聯碼|correlation/i);
    // ① 負向斷言：同一個節點不洩內部資訊。
    expect(text).not.toContain("services.jsx");
    expect(text).not.toContain("/srv/");
    expect(text).not.toContain("csp-internal");
    expect(text).not.toMatch(/\bat\s+\S+\s+\(/); // stack frame shape
    expect(text).not.toContain("Element type is invalid");
  });

  it("沒有例外時 children 原樣渲染（網子不改變正常路徑）", () => {
    render(
      <ErrorBoundary>
        <p>正常內容</p>
      </ErrorBoundary>,
    );
    expect(screen.getByText("正常內容")).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("重新整理按鈕存在且不清 localStorage 之外的東西（只重載）", () => {
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    );
    expect(screen.getByRole("button", { name: /重新整理/ })).toBeTruthy();
  });
});
