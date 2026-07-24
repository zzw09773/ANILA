// Composer 的觸發字元分派:`@` agent mention(零回歸)與新增的 `/` 斜線指令。
import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";

import { Composer } from "../chat.jsx";
import { ConfirmProvider } from "../confirm.jsx";

const AGENTS = [
  { id: "anila-router", name: "ANILA Router", short: "auto" },
  { id: "rag-agent", name: "知識檢索", short: "rag" },
  { id: "ocr-agent", name: "文件辨識", short: "ocr" },
];

function setup(props = {}) {
  const onSend = vi.fn();
  const utils = render(
    <ConfirmProvider>
      <Composer onSend={onSend} agents={AGENTS} {...props} />
    </ConfirmProvider>,
  );
  const textarea = utils.container.querySelector("textarea");
  return { onSend, textarea, ...utils };
}

// 直接改 textarea.value 會讓 React 的 value tracker 以為沒變 → 一律走
// fireEvent.change 的 target(RTL 會用原生 setter 正確觸發)。
function type(textarea, value, caret = value.length) {
  fireEvent.change(textarea, { target: { value, selectionStart: caret, selectionEnd: caret } });
}

beforeEach(() => {
  vi.restoreAllMocks();
  window.sessionStorage?.clear?.();
});

describe("@ mention(既有行為)", () => {
  it("shows real agents and hides the router pseudo-agent", () => {
    const { textarea } = setup();
    type(textarea, "@");
    expect(screen.getByRole("listbox", { name: "agent 建議" })).toBeInTheDocument();
    expect(screen.getByText("知識檢索")).toBeInTheDocument();
    expect(screen.getByText("文件辨識")).toBeInTheDocument();
    expect(screen.queryByText("ANILA Router")).not.toBeInTheDocument();
  });

  it("filters by the typed token", () => {
    const { textarea } = setup();
    type(textarea, "@ra");
    expect(screen.getByText("知識檢索")).toBeInTheDocument();
    expect(screen.queryByText("文件辨識")).not.toBeInTheDocument();
  });

  it("Enter picks the highlighted agent instead of sending", () => {
    const { textarea, onSend } = setup();
    type(textarea, "@ra");
    fireEvent.keyDown(textarea, { key: "Enter" });
    expect(onSend).not.toHaveBeenCalled();
    expect(textarea.value).toBe("@知識檢索 ");
  });

  it("Tab picks the highlighted agent too", () => {
    const { textarea } = setup();
    type(textarea, "@oc");
    fireEvent.keyDown(textarea, { key: "Tab" });
    expect(textarea.value).toBe("@文件辨識 ");
  });

  it("ArrowDown moves the highlight before confirming", () => {
    const { textarea } = setup();
    type(textarea, "@");
    fireEvent.keyDown(textarea, { key: "ArrowDown" });
    fireEvent.keyDown(textarea, { key: "Enter" });
    expect(textarea.value).toBe("@文件辨識 ");
  });

  it("Escape closes the menu and a following Enter sends normally", () => {
    const { textarea, onSend } = setup();
    type(textarea, "問問 @");
    expect(screen.getByRole("listbox", { name: "agent 建議" })).toBeInTheDocument();
    fireEvent.keyDown(textarea, { key: "Escape" });
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    fireEvent.keyDown(textarea, { key: "Enter" });
    expect(onSend).toHaveBeenCalledWith("問問 @", [], expect.anything());
  });

  it("plain text still sends on Enter", () => {
    const { textarea, onSend } = setup();
    type(textarea, "一般訊息");
    fireEvent.keyDown(textarea, { key: "Enter" });
    expect(onSend).toHaveBeenCalledWith("一般訊息", [], expect.anything());
  });
});

describe("/ 斜線指令", () => {
  it("opens the command menu on a leading slash", () => {
    const { textarea } = setup();
    type(textarea, "/");
    expect(screen.getByRole("listbox", { name: "斜線指令建議" })).toBeInTheDocument();
    expect(screen.getByText("/翻譯")).toBeInTheDocument();
    expect(screen.getByText("/清空")).toBeInTheDocument();
  });

  it("does not open mid-text (URL 不誤觸)", () => {
    const { textarea } = setup();
    type(textarea, "看 https://example/x");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("filters commands as you type", () => {
    const { textarea } = setup();
    type(textarea, "/清");
    expect(screen.getByText("/清空")).toBeInTheDocument();
    expect(screen.queryByText("/翻譯")).not.toBeInTheDocument();
  });

  it("/清空 clears the draft instead of sending", () => {
    const { textarea, onSend } = setup();
    type(textarea, "/清");
    fireEvent.keyDown(textarea, { key: "Enter" });
    expect(onSend).not.toHaveBeenCalled();
    expect(textarea.value).toBe("");
  });

  it("/快捷鍵 opens the shortcuts panel", () => {
    const onOpenShortcuts = vi.fn();
    const { textarea } = setup({ onOpenShortcuts });
    type(textarea, "/快捷鍵");
    fireEvent.keyDown(textarea, { key: "Enter" });
    expect(onOpenShortcuts).toHaveBeenCalledTimes(1);
  });

  it("/搜尋 opens the command palette", () => {
    const onOpenPalette = vi.fn();
    const { textarea } = setup({ onOpenPalette });
    type(textarea, "/搜尋");
    fireEvent.keyDown(textarea, { key: "Enter" });
    expect(onOpenPalette).toHaveBeenCalledTimes(1);
  });

  it("/翻譯 with an argument sends the templated prompt", () => {
    const { textarea, onSend } = setup();
    type(textarea, "/翻譯 這段話");
    // 打了空白選單已關 → 走 submit 路徑
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    fireEvent.keyDown(textarea, { key: "Enter" });
    expect(onSend).toHaveBeenCalledTimes(1);
    expect(onSend.mock.calls[0][0]).toContain("翻譯成英文");
    expect(onSend.mock.calls[0][0]).toContain("這段話");
  });

  it("/翻譯 with no argument applies to the latest reply when possible", () => {
    const onRunPromptAction = vi.fn(() => true);
    const { textarea, onSend } = setup({ onRunPromptAction });
    type(textarea, "/翻譯");
    fireEvent.keyDown(textarea, { key: "Enter" });
    expect(onRunPromptAction).toHaveBeenCalledTimes(1);
    expect(onSend).not.toHaveBeenCalled();
    expect(textarea.value).toBe("");
  });

  it("falls back to filling the instruction when there is no reply to act on", () => {
    const onRunPromptAction = vi.fn(() => false);
    const { textarea, onSend } = setup({ onRunPromptAction });
    type(textarea, "/摘要");
    fireEvent.keyDown(textarea, { key: "Enter" });
    expect(onSend).not.toHaveBeenCalled();
    expect(textarea.value).toContain("摘要成條列重點");
    expect(textarea.value).not.toContain("{content}");
  });

  it("hides quick-action commands for classified conversations", () => {
    const { textarea } = setup({ classified: true });
    type(textarea, "/");
    expect(screen.queryByText("/翻譯")).not.toBeInTheDocument();
    expect(screen.getByText("/清空")).toBeInTheDocument();
  });

  it("sends `/翻譯 x` as plain text when classified (不走快捷動作)", () => {
    const onRunPromptAction = vi.fn(() => true);
    const { textarea, onSend } = setup({ classified: true, onRunPromptAction });
    type(textarea, "/翻譯 機密內容");
    fireEvent.keyDown(textarea, { key: "Enter" });
    expect(onRunPromptAction).not.toHaveBeenCalled();
    expect(onSend).toHaveBeenCalledWith("/翻譯 機密內容", [], expect.anything());
  });

  it("surfaces CSP-defined prompt_action functions as commands", () => {
    const { textarea } = setup({
      messageActions: [
        { id: "legalese", label: "法務用語", config: { template: "改寫：{content}" } },
      ],
    });
    type(textarea, "/法務");
    expect(screen.getByText("/法務用語")).toBeInTheDocument();
  });

  it("clicking a command in the menu runs it", () => {
    const onOpenShortcuts = vi.fn();
    const { textarea } = setup({ onOpenShortcuts });
    type(textarea, "/快捷");
    act(() => {
      fireEvent.mouseDown(screen.getByText("/快捷鍵"));
    });
    expect(onOpenShortcuts).toHaveBeenCalledTimes(1);
  });

  it("unknown /commands still send as ordinary text", () => {
    const { textarea, onSend } = setup();
    type(textarea, "/不存在 參數");
    fireEvent.keyDown(textarea, { key: "Enter" });
    expect(onSend).toHaveBeenCalledWith("/不存在 參數", [], expect.anything());
  });
});
