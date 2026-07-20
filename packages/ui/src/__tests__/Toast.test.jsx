import React from "react";
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { Toast, ToastStack } from "../components/Toast.jsx";

describe("Toast", () => {
  it("以 status role 渲染訊息（aria-live polite）", () => {
    render(<Toast tone="success">已儲存設定</Toast>);
    const toast = screen.getByRole("status");
    expect(toast).toHaveTextContent("已儲存設定");
    expect(toast).toHaveAttribute("aria-live", "polite");
  });

  it("onClose 存在時顯示關閉鈕並可觸發", () => {
    const onClose = vi.fn();
    render(<Toast onClose={onClose}>訊息</Toast>);
    fireEvent.click(screen.getByRole("button", { name: "關閉" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("danger tone 使用語意色 token", () => {
    render(<Toast tone="danger">失敗</Toast>);
    expect(screen.getByRole("status").style.borderLeft).toContain(
      "--anila-color-danger",
    );
  });
});

describe("ToastStack", () => {
  it("疊放容器渲染子項", () => {
    render(
      <ToastStack data-testid="stack">
        <Toast>一</Toast>
        <Toast>二</Toast>
      </ToastStack>,
    );
    expect(screen.getByTestId("stack")).toBeInTheDocument();
    expect(screen.getAllByRole("status")).toHaveLength(2);
  });
});
