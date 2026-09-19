import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import { Modal } from "../components.jsx";

afterEach(cleanup);

describe("Shell Modal 短視窗與焦點", () => {
  it("開著時 Escape、背景與關閉鈕都會呼叫 onClose", () => {
    const onClose = vi.fn();
    const { rerender } = render(
      <Modal open onClose={onClose} title="設定" width={680}>
        <p>內容</p>
      </Modal>,
    );
    expect(screen.getByRole("dialog")).toBeTruthy();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);

    onClose.mockClear();
    fireEvent.click(screen.getByTitle("關閉"));
    expect(onClose).toHaveBeenCalledTimes(1);

    onClose.mockClear();
    fireEvent.click(screen.getByRole("dialog").parentElement);
    expect(onClose).toHaveBeenCalledTimes(1);

    rerender(
      <Modal open={false} onClose={onClose} title="設定">
        <p>內容</p>
      </Modal>,
    );
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("onClose 換新函式時不重設對話內焦點", () => {
    const first = vi.fn();
    const { rerender } = render(
      <Modal open onClose={first} title={"很長的對話標題".repeat(8)}>
        <input aria-label="欄位" />
      </Modal>,
    );
    const field = screen.getByLabelText("欄位");
    field.focus();
    expect(document.activeElement).toBe(field);

    rerender(
      <Modal open onClose={vi.fn()} title={"很長的對話標題".repeat(8)}>
        <input aria-label="欄位" />
      </Modal>,
    );
    expect(document.activeElement).toBe(field);
    expect(screen.getByTitle("關閉")).toBeTruthy();
    expect(screen.getByRole("dialog").style.maxWidth).toBe("480px");
  });
});
