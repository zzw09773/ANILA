// ANILA Shell 主導覽（Slice 9a）— 四大入口渲染、治理中心角色閘門、
// 外部同源連結（origin 絕對路徑）、專案入口開啟 ServicesPanel。

import React from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import {
  ANILA_LM_COMING_SOON_LABEL,
  ANILA_LM_ENTRY_ENABLED,
} from "../anilalmReleaseGate.js";
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
  it("returns the user entries in constitution order", () => {
    const entries = buildShellEntries({});
    expect(entries.map((e) => e.label)).toEqual([
      "對話",
      "我的知識庫",
      "專案入口",
      "用量",
    ]);
  });

  // 「產出中心」與「我的知識庫」曾是逐字相同的 /anilalm 連結：兩個標籤指到
  // 同一頁，使用者只會以為自己點錯。同一個 href 不得出現兩次。
  it("never ships two entries pointing at the same destination", () => {
    const hrefs = buildShellEntries({}).map((e) => e.href).filter(Boolean);
    expect(new Set(hrefs).size).toBe(hrefs.length);
  });

  it("gates 我的知識庫 behind ANILA_LM_ENTRY_ENABLED (Coming Soon when closed)", () => {
    const knowledge = buildShellEntries({}).find((e) => e.id === "knowledge");
    expect(knowledge).toBeTruthy();
    if (ANILA_LM_ENTRY_ENABLED) {
      expect(knowledge.href).toBe(`${ORIGIN}/anilalm`);
      expect(knowledge.disabled).toBeFalsy();
    } else {
      expect(knowledge.disabled).toBe(true);
      expect(knowledge.href).toBeUndefined();
      expect(knowledge.badge).toBe(ANILA_LM_COMING_SOON_LABEL);
    }
  });
});

// ---------------------------------------------------------------------
// 元件：ShellNav 渲染 + 閘門 + 連結 + 專案入口
// ---------------------------------------------------------------------

function openNav(user, extra = {}) {
  const utils = render(<ShellNav user={user} {...extra} />);
  fireEvent.click(screen.getByRole("button", { name: "平台入口" }));
  return utils;
}

describe("ShellNav", () => {
  it("hides the entries until the drawer is opened", () => {
    render(<ShellNav user={{ role: "user" }} />);
    expect(screen.getByRole("button", { name: "平台入口" })).toBeTruthy();
    expect(screen.queryByText("對話")).toBeNull();
    expect(screen.queryByText("我的知識庫")).toBeNull();
    expect(screen.queryByText("專案入口")).toBeNull();
    expect(screen.queryByText("用量")).toBeNull();
  });

  it("renders the user entries inside the modal", () => {
    openNav({ role: "user" });
    expect(screen.getByText("對話")).toBeTruthy();
    expect(screen.getByText("我的知識庫")).toBeTruthy();
    expect(screen.getByText("專案入口")).toBeTruthy();
    expect(screen.getByText("用量")).toBeTruthy();
  });

  it("no longer renders the duplicate 產出中心 entry", () => {
    openNav({ role: "user" });
    expect(screen.queryByText("產出中心")).toBeNull();
  });

  it("does not surface ANILALM / Studio / CSP tech brand names", () => {
    const { container } = openNav({ role: "owner" });
    const text = container.textContent || "";
    expect(text).not.toMatch(/ANILALM/i);
    expect(text).not.toMatch(/Studio/i);
    expect(text).not.toMatch(/\bCSP\b/);
  });

  it("hides 治理中心 for a non-admin user", () => {
    openNav({ role: "user" });
    expect(screen.queryByText("治理中心")).toBeNull();
  });

  it("shows 治理中心 for an admin and links it to the origin root", () => {
    openNav({ role: "admin" });
    const gov = screen.getByText("治理中心").closest("a");
    expect(gov).toBeTruthy();
    expect(gov.getAttribute("href")).toBe(`${ORIGIN}/`);
  });

  it("shows 我的知識庫 as disabled Coming Soon when the release gate is closed", () => {
    if (ANILA_LM_ENTRY_ENABLED) return; // gate open → this assertion does not apply
    openNav({ role: "user" });
    expect(screen.getByText("我的知識庫")).toBeTruthy();
    expect(screen.getByText(ANILA_LM_COMING_SOON_LABEL)).toBeTruthy();
    const row = screen.getByText("我的知識庫").closest("[data-nav-disabled='knowledge']");
    expect(row).toBeTruthy();
    expect(row.getAttribute("aria-disabled")).toBe("true");
    expect(screen.getByText("我的知識庫").closest("a")).toBeNull();
  });

  it("points 我的知識庫 at /anilalm only when the release gate is open", () => {
    openNav({ role: "user" });
    const link = screen.getByText("我的知識庫").closest("a");
    if (ANILA_LM_ENTRY_ENABLED) {
      expect(link).toBeTruthy();
      expect(link.getAttribute("href")).toBe(`${ORIGIN}/anilalm`);
    } else {
      expect(link).toBeNull();
    }
  });

  it("opens the ServicesPanel via onOpenServices when 專案入口 is clicked", () => {
    const onOpenServices = vi.fn();
    openNav({ role: "user" }, { onOpenServices });
    fireEvent.click(screen.getByText("專案入口"));
    expect(onOpenServices).toHaveBeenCalledTimes(1);
  });

  it("opens 我的用量 via onOpenUsage when 用量 is clicked", () => {
    const onOpenUsage = vi.fn();
    openNav({ role: "user" }, { onOpenUsage });
    fireEvent.click(screen.getByText("用量"));
    expect(onOpenUsage).toHaveBeenCalledTimes(1);
  });

  it("marks 對話 as the current entry and invokes onTaskCenter", () => {
    const onTaskCenter = vi.fn();
    openNav({ role: "user" }, { onTaskCenter });
    const tasks = screen.getByText("對話").closest("button");
    expect(tasks.getAttribute("aria-current")).toBe("page");
    fireEvent.click(tasks);
    expect(onTaskCenter).toHaveBeenCalledTimes(1);
  });

  it("renders an icon-only collapsed rail that still gates governance", () => {
    render(<ShellNav collapsed user={{ role: "user" }} />);
    fireEvent.click(screen.getByLabelText("平台入口"));
    expect(screen.getByText("對話")).toBeTruthy();
    expect(screen.getByText("專案入口")).toBeTruthy();
    expect(screen.queryByText("治理中心")).toBeNull();
  });
});
