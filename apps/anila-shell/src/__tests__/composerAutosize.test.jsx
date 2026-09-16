// 輸入框自動長高的不變式。
//
// 擁有者回報的畫面:口述一段 77 字的話,輸入框停在一行高,第二行被從字的中間
// 橫切掉,只看得到字的頭頂。根因是高度只在 onChange 裡重算,而語音定稿/切對話
// 回填草稿/提示詞範本這些路徑根本不經過 onChange。
//
// ⚠ jsdom 沒有排版引擎,`scrollHeight` 恆為 0 —— 直接寫測試會不管 production
// code 怎麼改都綠。所以這裡在 textarea 上裝一個**會隨 value 變動**的假
// scrollHeight(每 CHARS_PER_LINE 個字一行),讓量測那條路真的被走到。
// 每個 it 底下都註明「把哪一行改掉會讓它變紅」。

import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";

import { Composer, COMPOSER_LINE_HEIGHT, COMPOSER_MIN_ROWS, COMPOSER_MAX_ROWS } from "../chat.jsx";
import { ConfirmProvider } from "../confirm.jsx";

const CHARS_PER_LINE = 20;
const AGENTS = [{ id: "anila-router", name: "ANILA 自動選助手", short: "auto" }];

/** 裝上一個會隨 value 改變的假排版:N 個字 → ceil(N/20) 行 × 行高。 */
function fakeLayout(el, { charsPerLine = CHARS_PER_LINE, bleed = 0 } = {}) {
  Object.defineProperty(el, "scrollHeight", {
    configurable: true,
    get() {
      const lines = Math.max(1, Math.ceil(this.value.length / charsPerLine));
      return lines * COMPOSER_LINE_HEIGHT + bleed;
    },
  });
}

function renderComposer(props = {}) {
  const onSend = props.onSend || vi.fn();
  const utils = render(
    <ConfirmProvider>
      <Composer onSend={onSend} agents={AGENTS} {...props} />
    </ConfirmProvider>
  );
  const rerender = (next = {}) =>
    utils.rerender(
      <ConfirmProvider>
        <Composer onSend={onSend} agents={AGENTS} {...props} {...next} />
      </ConfirmProvider>
    );
  return { ...utils, rerender, onSend, ta: screen.getByRole("textbox") };
}

function heightPx(el) {
  return parseFloat(el.style.height);
}

beforeEach(() => {
  sessionStorage.clear();
  // useAsrInput 開場會 probe /asr/health;jsdom 沒有 fetch server。
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false })));
});

describe("Composer 自動長高", () => {
  it("空框預設一行，不要先撐成多行卡片", () => {
    expect(COMPOSER_MIN_ROWS).toBe(1);
  });

  it("空框與短字都撐在最小行數,多行才長高", () => {
    const { ta } = renderComposer();
    fakeLayout(ta);

    fireEvent.change(ta, { target: { value: "短" } });
    expect(heightPx(ta)).toBe(COMPOSER_LINE_HEIGHT * COMPOSER_MIN_ROWS);

    fireEvent.change(ta, { target: { value: "字".repeat(CHARS_PER_LINE * 3) } });
    expect(heightPx(ta)).toBe(COMPOSER_LINE_HEIGHT * 3);
  });

  it("打字超過最小行數,框就跟行數一起長", () => {
    const { ta } = renderComposer();
    fakeLayout(ta);

    fireEvent.change(ta, { target: { value: "字".repeat(CHARS_PER_LINE * 5) } });
    expect(heightPx(ta)).toBe(COMPOSER_LINE_HEIGHT * 5);
  });

  // 這是擁有者真正踩到的那一條:文字不是打進來的。
  it("文字從外面進來(切回對話回填草稿)也要重量高度", () => {
    const draft = "字".repeat(CHARS_PER_LINE * 4);
    sessionStorage.setItem("anila-draft:2", draft);

    const { ta, rerender } = renderComposer({ conversationId: 1 });
    fakeLayout(ta);

    // 先讓框停在最小高度(對話 1 的草稿是空的)。
    fireEvent.change(ta, { target: { value: "短" } });
    expect(heightPx(ta)).toBe(COMPOSER_LINE_HEIGHT * COMPOSER_MIN_ROWS);

    // 切到對話 2 → 草稿被塞進 state,完全沒有 onChange 事件。
    act(() => { rerender({ conversationId: 2 }); });

    expect(ta.value).toBe(draft);
    expect(heightPx(ta)).toBe(COMPOSER_LINE_HEIGHT * 4);
    // 「第二行被切一半」的具體定義:看得到的高度小於內容高度。
    expect(heightPx(ta)).toBeGreaterThanOrEqual(ta.scrollHeight);
  });

  it("點提示詞範本把整段填進來,框也要跟著長高", () => {
    const body = "字".repeat(CHARS_PER_LINE * 3);
    const { ta } = renderComposer({
      presetPrompts: [{ id: "p1", label: "範本", config: { text: body } }],
    });
    fakeLayout(ta);

    fireEvent.click(screen.getByRole("button", { name: "預設提示詞" }));
    fireEvent.click(screen.getByRole("button", { name: /範本/ }));

    expect(ta.value).toBe(body);
    expect(heightPx(ta)).toBe(COMPOSER_LINE_HEIGHT * 3);
  });

  it("送出以後縮回最小高度", () => {
    const onSend = vi.fn();
    const { ta } = renderComposer({ onSend });
    fakeLayout(ta);

    fireEvent.change(ta, { target: { value: "字".repeat(CHARS_PER_LINE * 5) } });
    expect(heightPx(ta)).toBe(COMPOSER_LINE_HEIGHT * 5);

    fireEvent.keyDown(ta, { key: "Enter" });

    expect(onSend).toHaveBeenCalledTimes(1);
    expect(ta.value).toBe("");
    expect(heightPx(ta)).toBe(COMPOSER_LINE_HEIGHT * COMPOSER_MIN_ROWS);
  });
});

describe("Composer 高度上限", () => {
  it("超過上限就停在整行邊界並開始捲動,不會切在字中間", () => {
    const { ta } = renderComposer();
    fakeLayout(ta);

    fireEvent.change(ta, {
      target: { value: "字".repeat(CHARS_PER_LINE * (COMPOSER_MAX_ROWS + 6)) },
    });

    const h = heightPx(ta);
    expect(h).toBe(COMPOSER_LINE_HEIGHT * COMPOSER_MAX_ROWS);
    // 上限必須是行高的整數倍 —— 否則最後一行只露出上半截。
    expect(h % COMPOSER_LINE_HEIGHT).toBe(0);
    expect(ta.style.overflowY).toBe("auto");
  });

  it("沒到上限時不出現捲軸", () => {
    const { ta } = renderComposer();
    fakeLayout(ta);
    fireEvent.change(ta, { target: { value: "字".repeat(CHARS_PER_LINE * 2) } });
    expect(ta.style.overflowY).toBe("hidden");
  });

  it("內容不到最小行數時也不出現捲軸", () => {
    const { ta } = renderComposer();
    fakeLayout(ta);
    fireEvent.change(ta, { target: { value: "短" } });
    expect(heightPx(ta)).toBe(COMPOSER_LINE_HEIGHT * COMPOSER_MIN_ROWS);
    expect(ta.style.overflowY).toBe("hidden");
  });

  it("內容高度帶零頭時進位到整行,不留半行", () => {
    const { ta } = renderComposer();
    // bleed:模擬子像素/邊框讓 scrollHeight 落在兩行之間(5 行 + 3px)。
    fakeLayout(ta, { bleed: 3 });

    fireEvent.change(ta, { target: { value: "字".repeat(CHARS_PER_LINE * 5) } });

    const h = heightPx(ta);
    expect(h % COMPOSER_LINE_HEIGHT).toBe(0);
    expect(h).toBe(COMPOSER_LINE_HEIGHT * 6);
    expect(h).toBeGreaterThanOrEqual(ta.scrollHeight);
  });
});
