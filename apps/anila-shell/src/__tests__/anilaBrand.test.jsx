// 品牌資產上線：側欄靜態 logo、空狀態英雄影片、路徑吃 BASE_URL。
import React from "react";
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

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
    agents: [],
    onOpenServices: noop,
    onTaskCenter: noop,
    user: { username: "tester", role: "user" },
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
