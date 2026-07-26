// 說明入口 + 快捷鍵面板(W1-9 ② / W1-10 ③)。
//
// 兩個缺陷綁在一起:
//   * 全 repo 零 help / 手冊 / FAQ,使用者沒有任何靜態求助路徑。
//   * 快捷鍵**只有**在鍵盤上按對才會知道 —— 自我指涉的可發現性 bug。
//
// 這裡釘死:① header 說明入口可點;② 面板列出的每個快捷鍵都指向本分支
// **真的存在**的按鍵處理;③ 面板章節與 `docs/guides/user-guide.md` 不漂移
// (逐章節比對真檔案,避免「文件說反話」第四個實例)。

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import React from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import {
  HELP_SECTIONS,
  HELP_HOTKEY_LABEL,
  SHORTCUTS,
  USER_GUIDE_PATH,
  HelpButton,
  HelpPanel,
  matchesHelpHotkey,
} from "../help.jsx";

// jsdom 的全域 `URL` 解相對路徑會落到 document base,不是 import.meta.url。
const hereDir = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(hereDir, "../../../..");
const guidePath = path.join(repoRoot, USER_GUIDE_PATH);

describe("HelpButton", () => {
  it("is a clickable header entry labelled 說明", () => {
    const onOpen = vi.fn();
    render(<HelpButton onOpen={onOpen} />);
    const button = screen.getByRole("button", { name: /說明/ });
    fireEvent.click(button);
    expect(onOpen).toHaveBeenCalledTimes(1);
  });
});

describe("HelpPanel", () => {
  it("renders nothing when closed", () => {
    const { container } = render(<HelpPanel open={false} onClose={() => {}} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("lists the shortcuts, including the one that opens itself", () => {
    render(<HelpPanel open onClose={() => {}} />);
    expect(screen.getByText(HELP_HOTKEY_LABEL)).toBeInTheDocument();
    for (const s of SHORTCUTS) {
      expect(screen.getByText(s.keys)).toBeInTheDocument();
    }
  });

  it("explains the copy/export restriction (密等行為)", () => {
    render(<HelpPanel open onClose={() => {}} />);
    expect(screen.getByText("複製與匯出限制")).toBeInTheDocument();
    const body = document.body.textContent || "";
    // 「為什麼」必須寫出來,不能只寫「不能複製」。
    expect(body).toMatch(/單向/);
    expect(body).toMatch(/稽核|紀錄/);
  });

  it("points at the full written guide", () => {
    render(<HelpPanel open onClose={() => {}} />);
    expect(screen.getByText(USER_GUIDE_PATH)).toBeInTheDocument();
  });
});

describe("matchesHelpHotkey", () => {
  it("matches Cmd+/ and Ctrl+/", () => {
    expect(matchesHelpHotkey({ key: "/", metaKey: true, ctrlKey: false })).toBe(true);
    expect(matchesHelpHotkey({ key: "/", metaKey: false, ctrlKey: true })).toBe(true);
  });

  it("ignores a bare slash (people type slashes)", () => {
    expect(matchesHelpHotkey({ key: "/", metaKey: false, ctrlKey: false })).toBe(false);
  });

  it("ignores other keys", () => {
    expect(matchesHelpHotkey({ key: "k", metaKey: true, ctrlKey: false })).toBe(false);
  });
});

// ── 文件漂移守門 ────────────────────────────────────────────────────────────
describe("docs/guides/user-guide.md", () => {
  const guide = fs.existsSync(guidePath) ? fs.readFileSync(guidePath, "utf8") : "";

  it("exists", () => {
    expect(fs.existsSync(guidePath)).toBe(true);
  });

  it("has the 複製與匯出限制 section the acceptance asks for", () => {
    expect(guide).toMatch(/^##+\s*複製與匯出限制/m);
  });

  it("covers every section the in-app panel shows (no drift)", () => {
    for (const section of HELP_SECTIONS) {
      expect(
        guide.includes(section.title),
        `user-guide.md 少了「${section.title}」章節 — 面板與文件已漂移`,
      ).toBe(true);
    }
  });

  it("cites a UI path for every claim block", () => {
    // W1-10 風險控制:文件內每個宣稱都要附對應 UI 路徑,否則就是下一個
    // 「文件說反話」。機械判準 = 每個 h2 段落內至少一條「UI 路徑:」。
    const sections = guide.split(/^## /m).slice(1);
    expect(sections.length).toBeGreaterThanOrEqual(4);
    for (const section of sections) {
      const heading = section.split("\n")[0].trim();
      expect(
        section.includes("UI 路徑:"),
        `「${heading}」章節沒有標出對應 UI 路徑`,
      ).toBe(true);
    }
  });

  it("documents every shortcut the panel claims", () => {
    for (const s of SHORTCUTS) {
      expect(guide.includes(s.keys), `user-guide.md 未記載 ${s.keys}`).toBe(true);
    }
  });
});
