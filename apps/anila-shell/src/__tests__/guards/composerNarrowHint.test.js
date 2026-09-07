// @source-text-guard
//
// 強度:**第三級(原始碼字串比對)**。窄螢幕藏 Enter 提示是 CSS，行為測試
// 掛不起 media query；這裡只擋整段 class / @media 被刪掉。
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const ROOT = resolve(process.cwd());

describe("composer 窄視窗", () => {
  it("hides the Enter hint below 520px so the send button stays in view", () => {
    const html = readFileSync(resolve(ROOT, "index.html"), "utf8");
    const chat = readFileSync(resolve(ROOT, "src/chat.jsx"), "utf8");
    expect(chat).toContain("composer-toolbar");
    expect(chat).toContain("composer-enter-hint");
    expect(html).toMatch(/@media \(max-width: 520px\)/);
    expect(html).toMatch(/\.composer-enter-hint \{ display: none; \}/);
  });
});
