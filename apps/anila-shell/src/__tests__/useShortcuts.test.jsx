// 全域快捷鍵 hook —— 焦點在輸入框內仍要能觸發,且不可搶掉純 Esc/Enter。
import React from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import { useShortcuts } from "../commands/useShortcuts.js";

function Harness({ handlers, enabled }) {
  useShortcuts(handlers, { enabled });
  return <input aria-label="probe" />;
}

describe("useShortcuts", () => {
  it("fires the registered global handlers", () => {
    const palette = vi.fn();
    const panel = vi.fn();
    const newChat = vi.fn();
    render(<Harness handlers={{
      "command-palette": palette,
      "shortcuts-panel": panel,
      "new-chat": newChat,
    }} />);

    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    expect(palette).toHaveBeenCalledTimes(1);

    fireEvent.keyDown(window, { key: "/", ctrlKey: true });
    expect(panel).toHaveBeenCalledTimes(1);

    fireEvent.keyDown(window, { key: "o", ctrlKey: true, shiftKey: true });
    expect(newChat).toHaveBeenCalledTimes(1);
  });

  it("still fires while focus is inside a text input", () => {
    const palette = vi.fn();
    render(<Harness handlers={{ "command-palette": palette }} />);
    const input = screen.getByLabelText("probe");
    input.focus();
    fireEvent.keyDown(input, { key: "k", ctrlKey: true, bubbles: true });
    expect(palette).toHaveBeenCalledTimes(1);
  });

  it("never claims bare Escape / Enter / plain letters", () => {
    const palette = vi.fn();
    render(<Harness handlers={{ "command-palette": palette }} />);
    fireEvent.keyDown(window, { key: "Escape" });
    fireEvent.keyDown(window, { key: "Enter" });
    fireEvent.keyDown(window, { key: "k" });
    expect(palette).not.toHaveBeenCalled();
  });

  it("ignores keys during IME composition", () => {
    const palette = vi.fn();
    render(<Harness handlers={{ "command-palette": palette }} />);
    fireEvent.keyDown(window, { key: "k", ctrlKey: true, isComposing: true });
    expect(palette).not.toHaveBeenCalled();
  });

  it("does nothing for shortcuts without a handler", () => {
    const palette = vi.fn();
    render(<Harness handlers={{ "command-palette": palette }} />);
    fireEvent.keyDown(window, { key: "o", ctrlKey: true, shiftKey: true });
    expect(palette).not.toHaveBeenCalled();
  });

  it("detaches when disabled and on unmount", () => {
    const palette = vi.fn();
    const { rerender, unmount } = render(
      <Harness handlers={{ "command-palette": palette }} enabled={false} />,
    );
    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    expect(palette).not.toHaveBeenCalled();

    rerender(<Harness handlers={{ "command-palette": palette }} enabled />);
    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    expect(palette).toHaveBeenCalledTimes(1);

    unmount();
    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    expect(palette).toHaveBeenCalledTimes(1);
  });
});
