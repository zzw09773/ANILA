// @source-text-guard
//
// 強度:**第三級(原始碼字串比對)**。排版是 CSS,jsdom 量不到寬度;這裡只擋
// 「狀態列又被搬回 toolbar 的平行欄位」或窄視窗把狀態藏掉。行為面見
// composerVoiceLayout.test.jsx。
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const ROOT = resolve(process.cwd());

describe("composer 窄視窗", () => {
  it("keeps the status line inside the input column instead of hiding it", () => {
    const html = readFileSync(resolve(ROOT, "index.html"), "utf8");
    const chat = readFileSync(resolve(ROOT, "src/chat.jsx"), "utf8");
    expect(chat).toContain("composer-toolbar");
    expect(chat).toContain("composer-status");
    expect(html).toMatch(/@media \(max-width: 520px\)/);
    // 狀態列(含「聆聽中…」)不能在窄視窗被藏:螢幕報讀和使用者都需要它。
    expect(html).not.toMatch(/\.composer-(enter-hint|status|live-status) \{ display: none; \}/);
  });
});
