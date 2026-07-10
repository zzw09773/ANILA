import React from "react";
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { Select } from "../components/Select.jsx";

const OPTIONS = [
  { value: "gemma4", label: "gemma4" },
  { value: "gpt-oss-20b", label: "gpt-oss-20b" },
];

describe("Select", () => {
  it("以 options 陣列渲染原生選項", () => {
    render(<Select label="模型" options={OPTIONS} defaultValue="gemma4" />);
    const select = screen.getByLabelText("模型");
    expect(select).toHaveValue("gemma4");
    expect(screen.getAllByRole("option")).toHaveLength(2);
  });

  it("變更選取時觸發 onChange", () => {
    const onChange = vi.fn();
    render(<Select label="模型" options={OPTIONS} onChange={onChange} />);
    fireEvent.change(screen.getByLabelText("模型"), {
      target: { value: "gpt-oss-20b" },
    });
    expect(onChange).toHaveBeenCalledTimes(1);
  });

  it("顯示 error 訊息", () => {
    render(<Select label="模型" options={OPTIONS} error="尚未選擇" />);
    expect(screen.getByText("尚未選擇")).toBeInTheDocument();
  });
});
