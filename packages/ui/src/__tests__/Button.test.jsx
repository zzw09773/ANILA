import React from "react";
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { Button, IconButton } from "../components/Button.jsx";

describe("Button", () => {
  it("渲染文字並回應點擊", () => {
    const onClick = vi.fn();
    render(<Button onClick={onClick}>儲存</Button>);
    const btn = screen.getByRole("button", { name: "儲存" });
    fireEvent.click(btn);
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("primary variant 使用官方藍 accent token", () => {
    render(<Button variant="primary">送出</Button>);
    const btn = screen.getByRole("button", { name: "送出" });
    expect(btn.style.background).toContain("--anila-color-accent");
  });

  it("渲染 leftIcon 與 rightIcon", () => {
    render(
      <Button leftIcon={<span data-testid="l" />} rightIcon={<span data-testid="r" />}>
        操作
      </Button>,
    );
    expect(screen.getByTestId("l")).toBeInTheDocument();
    expect(screen.getByTestId("r")).toBeInTheDocument();
  });
});

describe("IconButton", () => {
  it("title 鏡射為 aria-label（icon-only 可及性名稱）", () => {
    render(
      <IconButton title="設定">
        <svg />
      </IconButton>,
    );
    expect(screen.getByRole("button", { name: "設定" })).toBeInTheDocument();
  });
});
