import React from "react";
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { Tabs } from "../components/Tabs.jsx";

const TABS = [
  { id: "chats", label: "對話" },
  { id: "agents", label: "Agents" },
];

describe("Tabs", () => {
  it("渲染 tablist 並標記 aria-selected", () => {
    render(<Tabs tabs={TABS} value="chats" onChange={() => {}} aria-label="側欄分頁" />);
    expect(screen.getByRole("tablist", { name: "側欄分頁" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "對話" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "Agents" })).toHaveAttribute("aria-selected", "false");
  });

  it("點擊切換 value", () => {
    const onChange = vi.fn();
    render(<Tabs tabs={TABS} value="chats" onChange={onChange} />);
    fireEvent.click(screen.getByRole("tab", { name: "Agents" }));
    expect(onChange).toHaveBeenCalledWith("agents");
  });

  it("方向鍵循環切換", () => {
    const onChange = vi.fn();
    render(<Tabs tabs={TABS} value="agents" onChange={onChange} />);
    fireEvent.keyDown(screen.getByRole("tablist"), { key: "ArrowRight" });
    expect(onChange).toHaveBeenCalledWith("chats");
  });
});
