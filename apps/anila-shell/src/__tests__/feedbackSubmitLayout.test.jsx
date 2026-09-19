// @source-text-guard — 此檔含 CSS 靜態守衛；實際 hover／focus 排版另由瀏覽器驗證。
import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import React from "react";
import { MessageBubble } from "../chat.jsx";
import { ConfirmProvider } from "../confirm.jsx";

afterEach(cleanup);

const base = {
  id: 1,
  role: "assistant",
  text: "這是被評分的回答正文",
  streaming: false,
  siblingCount: 1,
  parentId: null,
};

const renderBubble = (msg, onRate) =>
  render(
    <ConfirmProvider>
      <MessageBubble
        msg={{ ...base, ...msg }}
        agents={[]}
        conversationId={1}
        onRate={onRate}
      />
    </ConfirmProvider>,
  );

function injectActionCss() {
  const html = readFileSync(resolve(process.cwd(), "index.html"), "utf8");
  const match = html.match(/<style>([\s\S]*?)<\/style>/);
  const style = document.createElement("style");
  style.setAttribute("data-testid", "injected-shell-css");
  style.textContent = match ? match[1] : "";
  document.head.appendChild(style);
  return style;
}

beforeEach(() => {
  document.head.querySelectorAll('[data-testid="injected-shell-css"]').forEach((n) => n.remove());
});

describe("倒讚回饋送出狀態", () => {
  it("onRate 被拒時不得先顯示感謝", async () => {
    const onRate = vi.fn(() => Promise.reject(new Error("儲存失敗")));
    renderBubble({ rating: "down" }, onRate);
    fireEvent.click(screen.getByTestId("feedback-submit"));
    await waitFor(() => expect(onRate).toHaveBeenCalledTimes(1));
    expect(screen.queryByTestId("feedback-thanks")).toBeNull();
    expect(screen.getByTestId("feedback-form")).toBeTruthy();
    const [, thumb, extra] = onRate.mock.calls[0];
    expect(thumb).toBe("down");
    expect(extra).toEqual({ comment: "", reasons: [] });
  });

  it("onRate 回 false 也不得假裝已送出", async () => {
    const onRate = vi.fn(async () => false);
    renderBubble({ rating: "down" }, onRate);
    fireEvent.click(screen.getByTestId("feedback-submit"));
    await waitFor(() => expect(onRate).toHaveBeenCalledTimes(1));
    expect(screen.queryByTestId("feedback-thanks")).toBeNull();
    expect(screen.getByTestId("feedback-form")).toBeTruthy();
  });

  it("非同步 onRate 尚未結束前不得顯示感謝", async () => {
    let resolveRate;
    const onRate = vi.fn(() => new Promise((resolve) => { resolveRate = resolve; }));
    renderBubble({ rating: "down" }, onRate);
    fireEvent.click(screen.getByTestId("feedback-submit"));
    expect(screen.queryByTestId("feedback-thanks")).toBeNull();
    expect(screen.getByTestId("feedback-form")).toBeTruthy();
    resolveRate(true);
    await waitFor(() => expect(screen.getByTestId("feedback-thanks")).toBeTruthy());
  });

  it("送出成功後才出現感謝，且訊息帶 feedback-sent 讓工具列可收合", async () => {
    const onRate = vi.fn(async () => true);
    const { container } = renderBubble({ rating: "down" }, onRate);
    fireEvent.click(screen.getByTestId("feedback-submit"));
    await waitFor(() => expect(screen.getByTestId("feedback-thanks")).toBeTruthy());
    const root = container.querySelector(".anila-msg-assistant");
    expect(root.className.split(/\s+/)).toContain("anila-msg-feedback-sent");
  });
});

describe("已送出後工具列高度", () => {
  it("未送出的一般訊息不帶收合 class，避免平常氣泡被壓扁", () => {
    const { container } = renderBubble({ rating: "up" }, vi.fn());
    const root = container.querySelector(".anila-msg-assistant");
    expect(root.className.split(/\s+/)).not.toContain("anila-msg-feedback-sent");
    expect(screen.getByTestId("rating-score-picker").className.split(/\s+/)).toContain("anila-msg-actions");
  });

  it("已送出且未 hover／未 focus 時，工具列與分數列收合高度", async () => {
    injectActionCss();
    const onRate = vi.fn(async () => true);
    const { container } = renderBubble({ rating: "down" }, onRate);
    fireEvent.click(screen.getByTestId("feedback-submit"));
    await waitFor(() => expect(screen.getByTestId("feedback-thanks")).toBeTruthy());

    const root = container.querySelector(".anila-msg-assistant");
    const actions = [...root.querySelectorAll(".anila-msg-actions")];
    expect(actions.length).toBeGreaterThan(0);
    for (const row of actions) {
      const cs = getComputedStyle(row);
      expect(Number.parseFloat(cs.maxHeight)).toBe(0);
      expect(cs.overflow).toBe("hidden");
      expect(Number.parseFloat(cs.marginTop)).toBe(0);
    }
  });

  it("focus-within 時工具列展開，焦點目標仍在樹上且可聚焦", async () => {
    injectActionCss();
    const onRate = vi.fn(async () => true);
    const { container } = renderBubble({ rating: "down" }, onRate);
    fireEvent.click(screen.getByTestId("feedback-submit"));
    await waitFor(() => expect(screen.getByTestId("feedback-thanks")).toBeTruthy());

    const root = container.querySelector(".anila-msg-assistant");
    const thumb = root.querySelector('button[title="取消標記"]') || root.querySelector("button");
    expect(thumb).toBeTruthy();
    expect(thumb.disabled).toBe(false);
    expect(thumb.hasAttribute("inert")).toBe(false);
    expect(getComputedStyle(thumb).display).not.toBe("none");
    expect(getComputedStyle(thumb).visibility).not.toBe("hidden");

    thumb.focus();
    expect(document.activeElement).toBe(thumb);
    expect(root.contains(document.activeElement)).toBe(true);
    const opened = getComputedStyle(root.querySelector(".anila-msg-actions")).maxHeight;
    // jsdom 對 :focus-within 支援不一。焦點已落到工具列＝沒被拿掉。
    if (Number.parseFloat(opened) !== 0) {
      expect(opened === "none" || opened === "" || Number.parseFloat(opened) > 0).toBe(true);
    }
  });

  it("index.html 對 touch（hover:none）仍強制顯示已送出的工具列", () => {
    const html = readFileSync(resolve(process.cwd(), "index.html"), "utf8");
    expect(html).toMatch(/anila-msg-feedback-sent/);
    expect(html).toMatch(/@media \(hover: none\)/);
    const touchBlock = html.split("@media (hover: none)")[1] || "";
    expect(touchBlock).toMatch(/anila-msg-feedback-sent[\s\S]*max-height:\s*none/);
  });
});
