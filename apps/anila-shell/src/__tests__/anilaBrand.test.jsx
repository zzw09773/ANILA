// 品牌資產上線：側欄靜態 logo、空狀態英雄影片、路徑吃 BASE_URL。
import React from "react";
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import {
  ANILA_LOGO_MP4,
  ANILA_LOGO_PNG,
  ANILA_MARK_PNG,
  AnilaLogoImg,
  AnilaLogoVideo,
} from "../AnilaBrand.jsx";
import { EmptyState } from "../app.jsx";
import { Sidebar } from "../chat.jsx";
import { ConfirmProvider } from "../confirm.jsx";
import { DEFAULT_FOLDERS } from "../data.jsx";

describe("AnilaBrand helpers", () => {
  it("asset urls contain brand/anila-", () => {
    expect(ANILA_LOGO_PNG).toContain("brand/anila-logo.png");
    expect(ANILA_MARK_PNG).toContain("brand/anila-mark.png");
    expect(ANILA_LOGO_MP4).toContain("brand/anila-logo.mp4");
  });

  it("LogoImg renders the full logo png", () => {
    render(<AnilaLogoImg />);
    expect(screen.getByRole("img", { name: "ANILA" }).getAttribute("src"))
      .toContain("brand/anila-logo.png");
  });

  it("LogoVideo is muted, inline, no controls, brand src", () => {
    const { container } = render(<AnilaLogoVideo />);
    const video = container.querySelector("video");
    expect(video).toBeTruthy();
    expect(video.getAttribute("src")).toContain("brand/anila-logo.mp4");
    expect(video.getAttribute("aria-label")).toBe("ANILA");
    expect(video.muted).toBe(true);
    expect(video.autoplay).toBe(true);
    expect(video.hasAttribute("controls")).toBe(false);
    expect(video.loop).toBe(true);
    // mix-blend-mode 由 CSS 管：暗色才能覆寫 multiply，不要寫死在 inline。
    expect(video.style.mixBlendMode).toBe("");
  });
});

describe("EmptyState brand hero", () => {
  it("shows a static brand mark without autoplay above the title", () => {
    render(
      <EmptyState
        agent={{ id: "anila-router", name: "ANILA" }}
        agents={[]}
        onPick={() => {}}
      />,
    );
    expect(screen.getByText("你今天想問 ANILA 什麼？")).toBeTruthy();
    expect(screen.getByRole("img", { name: "ANILA" }).getAttribute("src")).toContain("brand/anila-mark.png");
    expect(document.querySelector("video")).toBeNull();
  });
});

describe("Sidebar brand", () => {
  const noop = () => {};
  const props = {
    conversations: [],
    selectedConvId: null,
    onSelectConv: noop,
    onNewChat: noop,
    onHome: noop,
    agents: [],
    onOpenServices: noop,
    onTaskCenter: noop,
    user: { id: 1, username: "tester", role: "user" },
    onLogout: noop,
    onOpenSettings: noop,
    collapsed: false,
    onToggleCollapsed: noop,
    folder: "all",
    setFolder: noop,
    folders: DEFAULT_FOLDERS,
  };

  it("expanded sidebar shows the mark plus ANILA word to the right", () => {
    render(
      <ConfirmProvider>
        <Sidebar {...props} />
      </ConfirmProvider>,
    );
    const img = screen.getByRole("img", { name: "ANILA" });
    expect(img.getAttribute("src")).toContain("brand/anila-mark.png");
    expect(screen.getByText("ANILA")).toBeTruthy();
  });

  it("collapsed sidebar still uses a brand png", () => {
    render(
      <ConfirmProvider>
        <Sidebar {...props} collapsed />
      </ConfirmProvider>,
    );
    const img = screen.getByRole("img", { name: "ANILA" });
    expect(img.getAttribute("src")).toContain("brand/anila-logo.png");
  });

  it("expanded mark and ANILA are one 回到首頁 button that calls onHome, not toggle", () => {
    const onHome = vi.fn();
    const onToggleCollapsed = vi.fn();
    const onNewChat = vi.fn();
    render(
      <ConfirmProvider>
        <Sidebar {...props} onHome={onHome} onToggleCollapsed={onToggleCollapsed} onNewChat={onNewChat} />
      </ConfirmProvider>,
    );
    const home = screen.getByRole("button", { name: "回到首頁" });
    expect(home.querySelector("img")?.getAttribute("src")).toContain("brand/anila-mark.png");
    expect(home.textContent).toContain("ANILA");
    expect(home.style.background).toBe("transparent");
    expect(home.style.borderWidth).toBe("0px");
    expect(parseFloat(home.style.minWidth) || 0).toBeGreaterThanOrEqual(24);
    expect(parseFloat(home.style.minHeight) || 0).toBeGreaterThanOrEqual(24);
    fireEvent.click(home);
    expect(onHome).toHaveBeenCalledTimes(1);
    expect(onToggleCollapsed).not.toHaveBeenCalled();
    expect(onNewChat).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: /新對話/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: "收合側邊" })).toBeTruthy();
  });

  it("collapsed brand is 回到首頁 and the adjacent chevron still expands", () => {
    const onHome = vi.fn();
    const onToggleCollapsed = vi.fn();
    render(
      <ConfirmProvider>
        <Sidebar {...props} collapsed onHome={onHome} onToggleCollapsed={onToggleCollapsed} />
      </ConfirmProvider>,
    );
    const home = screen.getByRole("button", { name: "回到首頁" });
    expect(home.querySelector("img")?.getAttribute("src")).toContain("brand/anila-logo.png");
    const expand = screen.getByRole("button", { name: "展開側邊" });
    expect(expand).not.toBe(home);
    fireEvent.click(home);
    expect(onHome).toHaveBeenCalledTimes(1);
    expect(onToggleCollapsed).not.toHaveBeenCalled();
    fireEvent.click(expand);
    expect(onToggleCollapsed).toHaveBeenCalledTimes(1);
    expect(onHome).toHaveBeenCalledTimes(1);
  });

  it("brand home is disabled while a bulk action is in progress, same as 新對話", async () => {
    let release;
    const pending = new Promise((resolve) => {
      release = resolve;
    });
    render(
      <ConfirmProvider>
        <Sidebar
          {...props}
          conversations={[{ id: 55, title: "對話甲", updatedAt: "2026-10-02T00:00:00Z", folder: "all", tags: [] }]}
          onBulkDelete={() => pending}
        />
      </ConfirmProvider>,
    );
    fireEvent.click(screen.getByRole("button", { name: "多選" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "選取對話 對話甲" }));
    fireEvent.click(screen.getByRole("button", { name: "刪除" }));
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "回到首頁" }).disabled).toBe(true);
      expect(screen.getByRole("button", { name: /新對話/ }).disabled).toBe(true);
    });
    release({ succeeded: [], failed: [] });
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "回到首頁" }).disabled).toBe(false);
      expect(screen.getByRole("button", { name: /新對話/ }).disabled).toBe(false);
    });
  });

  it("keeps the 對話 list and does not render an Agents tab", () => {
    render(
      <ConfirmProvider>
        <Sidebar {...props} />
      </ConfirmProvider>,
    );
    expect(screen.getByText("對話")).toBeTruthy();
    expect(screen.queryByText("Agents")).toBeNull();
  });
});
