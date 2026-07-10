import React from "react";
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { NavGroup, NavItem } from "../components/Nav.jsx";
import { Icon } from "../icons.jsx";

const TestIcon = (p) => <Icon {...p} />;

describe("NavItem", () => {
  it("href 走 <a> 並帶 title", () => {
    render(
      <NavGroup aria-label="測試導覽">
        <NavItem icon={TestIcon} label="我的知識庫" href="https://host/anilalm" />
      </NavGroup>,
    );
    const link = screen.getByRole("link", { name: "我的知識庫" });
    expect(link).toHaveAttribute("href", "https://host/anilalm");
  });

  it("onClick 走 <button> 並可觸發", () => {
    const onClick = vi.fn();
    render(<NavItem icon={TestIcon} label="專案入口" onClick={onClick} />);
    fireEvent.click(screen.getByRole("button", { name: "專案入口" }));
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("current 標記 aria-current=page", () => {
    render(<NavItem icon={TestIcon} label="任務中心" onClick={() => {}} current />);
    expect(screen.getByRole("button", { name: "任務中心" })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  it("collapsed 時仍保留 aria-label（icon-only 可及性）", () => {
    render(<NavItem icon={TestIcon} label="任務中心" onClick={() => {}} collapsed />);
    expect(screen.getByRole("button", { name: "任務中心" })).toBeInTheDocument();
  });
});

describe("NavGroup", () => {
  it("渲染 nav landmark", () => {
    render(<NavGroup aria-label="主導覽">x</NavGroup>);
    expect(screen.getByRole("navigation", { name: "主導覽" })).toBeInTheDocument();
  });
});
