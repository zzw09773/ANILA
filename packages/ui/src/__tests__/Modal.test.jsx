import React from "react";
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { Modal } from "../components/Modal.jsx";

describe("Modal", () => {
  it("open=false 不渲染", () => {
    render(
      <Modal open={false} onClose={() => {}} title="標題">
        內容
      </Modal>,
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("open=true 渲染 dialog 與標題/副標", () => {
    render(
      <Modal open onClose={() => {}} title="刪除確認" subtitle="動作無法復原">
        內容
      </Modal>,
    );
    const dialog = screen.getByRole("dialog", { name: "刪除確認" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(screen.getByText("動作無法復原")).toBeInTheDocument();
  });

  it("ESC 觸發 onClose", () => {
    const onClose = vi.fn();
    render(
      <Modal open onClose={onClose} title="標題">
        內容
      </Modal>,
    );
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
