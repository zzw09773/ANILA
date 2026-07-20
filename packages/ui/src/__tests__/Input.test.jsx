import React from "react";
import { describe, expect, it } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { Input } from "../components/Input.jsx";

describe("Input", () => {
  it("渲染 label 並可輸入", () => {
    render(<Input label="名稱" defaultValue="測試" />);
    const input = screen.getByLabelText("名稱");
    expect(input).toHaveValue("測試");
  });

  it("error 優先於 hint 顯示", () => {
    render(<Input label="名稱" hint="提示文字" error="必填欄位" />);
    expect(screen.getByText("必填欄位")).toBeInTheDocument();
    expect(screen.queryByText("提示文字")).not.toBeInTheDocument();
  });

  it("無 error 時顯示 hint", () => {
    render(<Input label="名稱" hint="提示文字" />);
    expect(screen.getByText("提示文字")).toBeInTheDocument();
  });
  it("focus 與 blur 以 React state 切換邊框 token", () => {
    render(<Input label="名稱" />);
    const input = screen.getByLabelText("名稱");
    const wrapper = input.parentElement;
    fireEvent.focus(input);
    expect(wrapper.style.border).toContain("--anila-color-accent");
    fireEvent.blur(input);
    expect(wrapper.style.border).toContain("--anila-color-border");
  });
});
