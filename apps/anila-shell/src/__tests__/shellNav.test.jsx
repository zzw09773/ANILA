// ANILA Shell 主導覽（Slice 9a）— 四大入口渲染、治理中心角色閘門、
// 外部同源連結（origin 絕對路徑）、專案入口開啟 ServicesPanel。

import React from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import {
  ShellNav,
  canSeeGovernance,
  originHref,
  buildShellEntries,
} from "../shellNav.jsx";

// jsdom 預設 origin。
const ORIGIN = window.location.origin;

// ---------------------------------------------------------------------
// 純函式：治理中心角色閘門
// ---------------------------------------------------------------------

describe("canSeeGovernance", () => {
  it("shows for owner / admin / developer", () => {
    expect(canSeeGovernance({ role: "owner" })).toBe(true);
    expect(canSeeGovernance({ role: "admin" })).toBe(true);
    expect(canSeeGovernance({ role: "developer" })).toBe(true);
  });

  it("hides for a plain user or a system account", () => {
    expect(canSeeGovernance({ role: "user" })).toBe(false);
    expect(canSeeGovernance({ role: "system" })).toBe(false);
  });

  it("defensively hides when the role is unknown or the user is null", () => {
    expect(canSeeGovernance(null)).toBe(false);
    expect(canSeeGovernance(undefined)).toBe(false);
    expect(canSeeGovernance({})).toBe(false);
    expect(canSeeGovernance({ role: 42 })).toBe(false);
  });
});

// ---------------------------------------------------------------------
// 純函式：origin 絕對路徑
// ---------------------------------------------------------------------

describe("originHref", () => {
  it("prefixes the current origin, not the shell /anila/ base", () => {
    expect(originHref("/anilalm")).toBe(`${ORIGIN}/anilalm`);
    expect(originHref("/")).toBe(`${ORIGIN}/`);
  });
});

describe("buildShellEntries", () => {
  it("returns the four user entries in constitution order", () => {
    const entries = buildShellEntries({});
    expect(entries.map((e) => e.label)).toEqual([
      "任務中心",
      "我的知識庫",
      "產出中心",
      "專案入口",
    ]);
    // 知識庫 / 產出中心皆連向同源 /anilalm。
    expect(entries[1].href).toBe(`${ORIGIN}/anilalm`);
    expect(entries[2].href).toBe(`${ORIGIN}/anilalm`);
  });
});

// ---------------------------------------------------------------------
// 元件：ShellNav 渲染 + 閘門 + 連結 + 專案入口
// ---------------------------------------------------------------------

describe("ShellNav", () => {
  it("renders the four user entries", () => {
    render(<ShellNav user={{ role: "user" }} />);
    expect(screen.getByText("任務中心")).toBeTruthy();
    expect(screen.getByText("我的知識庫")).toBeTruthy();
    expect(screen.getByText("產出中心")).toBeTruthy();
    expect(screen.getByText("專案入口")).toBeTruthy();
  });

  it("does not surface ANILALM / Studio / CSP tech brand names", () => {
    const { container } = render(<ShellNav user={{ role: "owner" }} />);
    const text = container.textContent || "";
    expect(text).not.toMatch(/ANILALM/i);
    expect(text).not.toMatch(/Studio/i);
    expect(text).not.toMatch(/\bCSP\b/);
  });

  it("hides 治理中心 for a non-admin user", () => {
    render(<ShellNav user={{ role: "user" }} />);
    expect(screen.queryByText("治理中心")).toBeNull();
  });

  it("shows 治理中心 for an admin and links it to the origin root", () => {
    render(<ShellNav user={{ role: "admin" }} />);
    const gov = screen.getByText("治理中心").closest("a");
    expect(gov).toBeTruthy();
    expect(gov.getAttribute("href")).toBe(`${ORIGIN}/`);
  });

  it("points knowledge and output at the same-origin /anilalm surface", () => {
    render(<ShellNav user={{ role: "user" }} />);
    expect(screen.getByText("我的知識庫").closest("a").getAttribute("href")).toBe(`${ORIGIN}/anilalm`);
    expect(screen.getByText("產出中心").closest("a").getAttribute("href")).toBe(`${ORIGIN}/anilalm`);
  });

  it("opens the ServicesPanel via onOpenServices when 專案入口 is clicked", () => {
    const onOpenServices = vi.fn();
    render(<ShellNav user={{ role: "user" }} onOpenServices={onOpenServices} />);
    fireEvent.click(screen.getByText("專案入口"));
    expect(onOpenServices).toHaveBeenCalledTimes(1);
  });

  it("marks 任務中心 as the current entry and invokes onTaskCenter", () => {
    const onTaskCenter = vi.fn();
    render(<ShellNav user={{ role: "user" }} onTaskCenter={onTaskCenter} />);
    const tasks = screen.getByText("任務中心").closest("button");
    expect(tasks.getAttribute("aria-current")).toBe("page");
    fireEvent.click(tasks);
    expect(onTaskCenter).toHaveBeenCalledTimes(1);
  });

  it("renders an icon-only collapsed rail that still gates governance", () => {
    render(<ShellNav collapsed user={{ role: "user" }} />);
    // 折疊時以 aria-label 提供無障礙名稱。
    expect(screen.getByLabelText("任務中心")).toBeTruthy();
    expect(screen.getByLabelText("專案入口")).toBeTruthy();
    expect(screen.queryByLabelText("治理中心")).toBeNull();
  });
});
