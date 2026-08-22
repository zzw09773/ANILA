// ANILA Shell 主導覽（Slice 9a）— 四大入口渲染、治理中心角色閘門、
// 外部同源連結（origin 絕對路徑）、專案入口開啟 ServicesPanel。

import React from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import { ANILA_LM_ENTRY_ENABLED } from "../anilalmReleaseGate.js";
import {
  ShellNav,
  canSeeGovernance,
  originHref,
  buildShellEntries,
} from "../shellNav.jsx";

const GOV_ORIGIN = `${window.location.protocol}//${window.location.hostname}:5173`;
const KNOWLEDGE_ORIGIN = `${window.location.protocol}//${window.location.hostname}:5174`;

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
    expect(originHref("/anilalm")).toBe(`${KNOWLEDGE_ORIGIN}/`);
    expect(originHref("/")).toBe(`${GOV_ORIGIN}/`);
  });
});

describe("buildShellEntries", () => {
  it("returns the user entries in constitution order", () => {
    const entries = buildShellEntries({});
    expect(entries.map((e) => e.label)).toEqual([
      "任務中心",
      "我的知識庫",
      "產出中心",
      "專案入口",
    ]);
  });

  // 產出中心有自己的 /outputs 頁，不可和知識庫指向同一個 URL。
  it("never ships two entries pointing at the same destination", () => {
    const hrefs = buildShellEntries({}).map((e) => e.href).filter(Boolean);
    expect(new Set(hrefs).size).toBe(hrefs.length);
  });

  it("opens 我的知識庫 in this release", () => {
    expect(ANILA_LM_ENTRY_ENABLED).toBe(true);
    const knowledge = buildShellEntries({}).find((e) => e.id === "knowledge");
    expect(knowledge).toBeTruthy();
    expect(knowledge.href).toBe(`${KNOWLEDGE_ORIGIN}/`);
    expect(knowledge.disabled).toBeFalsy();
    const outputs = buildShellEntries({}).find((e) => e.id === "outputs");
    expect(outputs.href).toBe(`${KNOWLEDGE_ORIGIN}/outputs`);
  });
});

// ---------------------------------------------------------------------
// 元件：ShellNav 渲染 + 閘門 + 連結 + 專案入口
// ---------------------------------------------------------------------

describe("ShellNav", () => {
  it("renders the user entries", () => {
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

  it("hides 系統管理 for a non-admin user", () => {
    render(<ShellNav user={{ role: "user" }} />);
    expect(screen.queryByText("系統管理")).toBeNull();
  });

  it("shows 系統管理 for an admin and links it to the configured governance origin", () => {
    render(<ShellNav user={{ role: "admin" }} />);
    const gov = screen.getByText("系統管理").closest("a");
    expect(gov).toBeTruthy();
    expect(gov.getAttribute("href")).toBe(`${GOV_ORIGIN}/`);
  });

  it("points 我的知識庫 and 產出中心 at distinct pages", () => {
    render(<ShellNav user={{ role: "user" }} />);
    const knowledge = screen.getByText("我的知識庫").closest("a");
    const outputs = screen.getByText("產出中心").closest("a");
    expect(knowledge.getAttribute("href")).toBe(`${KNOWLEDGE_ORIGIN}/`);
    expect(outputs.getAttribute("href")).toBe(`${KNOWLEDGE_ORIGIN}/outputs`);
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
    expect(screen.getByLabelText("產出中心")).toBeTruthy();
    expect(screen.getByLabelText("專案入口")).toBeTruthy();
    expect(screen.queryByLabelText("系統管理")).toBeNull();
  });
});
