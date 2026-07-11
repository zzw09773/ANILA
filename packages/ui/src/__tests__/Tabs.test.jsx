import React, { useState } from "react";
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

  it("方向鍵切換選取並把焦點移到新 tab", () => {
    function Harness() {
      const [value, setValue] = useState("agents");
      return <Tabs tabs={TABS} value={value} onChange={setValue} />;
    }

    render(<Harness />);
    const agents = screen.getByRole("tab", { name: "Agents" });
    agents.focus();
    fireEvent.keyDown(agents, { key: "ArrowRight" });

    const chats = screen.getByRole("tab", { name: "對話" });
    expect(chats).toHaveFocus();
    expect(chats).toHaveAttribute("aria-selected", "true");
  });
});
