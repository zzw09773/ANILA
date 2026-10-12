// 語音輸入時 composer 的排版不變式。
//
// 擁有者回報的畫面:聆聽中時,即時預覽(灰色斜體)貼在外框頂端像畫在框外;
// 「36 字 · ANILA 會幫你找合適的助手」和「聆聽中…」變成 toolbar 裡和 textarea
// 平行的欄位,把輸入框擠成中間窄窄一欄,而且 footer 文字跟框外那行重複。
// jsdom 量不到寬度,所以這裡鎖 DOM 結構:預覽和狀態列都必須在輸入欄
// (textarea 的同一個容器)裡,而不是 toolbar 的直接子元素。
import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

const asrState = { state: "listening", partial: "好 沒關係 這個到時候" };
vi.mock("../asr/useAsrInput.js", async (orig) => {
  const real = await orig();
  return {
    ...real,
    useAsrInput: () => ({
      available: true,
      state: asrState.state,
      partial: asrState.partial,
      error: null,
      clearError: () => {},
      toggle: () => {},
      onCompositionStart: () => {},
      onCompositionEnd: () => {},
    }),
  };
});

import { Composer } from "../chat.jsx";
import { ConfirmProvider } from "../confirm.jsx";

const AGENTS = [{ id: "anila-router", name: "ANILA", short: "auto" }];
const FOOTER = "ANILA 會幫你找合適的助手";

function renderComposer() {
  render(
    <ConfirmProvider>
      <Composer onSend={vi.fn()} agents={AGENTS} footer={FOOTER} />
    </ConfirmProvider>
  );
  return screen.getByRole("textbox");
}

beforeEach(() => {
  sessionStorage.clear();
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false })));
  asrState.state = "listening";
  asrState.partial = "好 沒關係 這個到時候";
});

describe("Composer 語音輸入排版", () => {
  it("即時預覽在輸入欄內(textarea 下方),不在外框頂端", () => {
    const ta = renderComposer();
    const col = ta.parentElement;
    const partial = screen.getByTestId("asr-partial");
    expect(partial.textContent).toBe("好 沒關係 這個到時候");
    expect(col.contains(partial)).toBe(true);
    // textarea 在前、預覽在後
    expect(ta.compareDocumentPosition(partial) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("字數與聆聽中在輸入欄的狀態列,不是 toolbar 裡和 textarea 搶寬的欄位", () => {
    const ta = renderComposer();
    fireEvent.change(ta, { target: { value: "做好了 如果你那邊要改" } });
    const col = ta.parentElement;
    const toolbar = col.parentElement;
    expect(toolbar.className).toBe("composer-toolbar");
    const status = screen.getByTestId("composer-status");
    expect(col.contains(status)).toBe(true);
    // toolbar 的直接子元素裡不能再有字數/狀態文字
    for (const child of toolbar.children) {
      if (child === col) continue;
      expect(child.textContent).not.toMatch(/字|聆聽中/);
    }
    expect(status.textContent).toContain("11 字");
    const live = screen.getByTestId("composer-live-status");
    expect(live.getAttribute("aria-live")).toBe("polite");
    expect(live.textContent).toBe("聆聽中…");
  });

  it("footer 提示不在 composer 內重複(框外那行已經有)", () => {
    const ta = renderComposer();
    fireEvent.change(ta, { target: { value: "hello" } });
    expect(screen.getByTestId("composer-status").textContent).not.toContain(FOOTER);
  });

  it("沒在聆聽時 live region 仍掛著但是空的,預覽不出現", () => {
    asrState.state = "idle";
    asrState.partial = "";
    renderComposer();
    expect(screen.queryByTestId("asr-partial")).toBeNull();
    expect(screen.getByTestId("composer-live-status").textContent).toBe("");
  });
});
