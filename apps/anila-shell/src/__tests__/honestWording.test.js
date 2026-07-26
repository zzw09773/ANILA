// 字面誠實化的機械守門(W1-3 ① / W1-9 ①②)。
//
// 根因(稽核 S5 一級缺陷類別):全 repo 零 at-rest 加密
// (`pgcrypto|LUKS|dm-crypt|TDE` grep=0),而 UI 把 `requires_encryption` 的
// 單向密等鎖定講成「加密」+「模式」。這支測試把措辭鎖住,避免回歸。
//
// 注意:本檔**刻意**不寫出被禁字樣的連續字串(用陣列拼),否則
// `grep -rn` 的驗收指令會在測試檔自己身上命中。

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { describe, it, expect } from "vitest";

import { CHANGELOG, CHANGELOG_VERSION } from "../changelog.jsx";

// jsdom 的全域 `URL` 會把相對路徑解到 document base(http://localhost:3000),
// 不是 import.meta.url —— 所以先轉成檔案路徑再用 path 往上走。
const hereDir = path.dirname(fileURLToPath(import.meta.url));
const srcDir = path.resolve(hereDir, "..");

// 每一條都是「加密 + X」的組合;分開寫,grep 抓不到本檔。
const FORBIDDEN = [
  ["加密", "模式"],
  ["加密", "模型"],
  ["加密", "對話"],
  ["加密", "記憶"],
  ["加密", "來源"],
  ["加密", "設定"],
].map((parts) => parts.join(""));

function walk(dir) {
  const out = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...walk(full));
    else if (/\.(jsx?|mjs)$/.test(entry.name)) out.push(full);
  }
  return out;
}

const sourceFiles = walk(srcDir);

describe("shell 原始碼不得再稱密等鎖定為「加密」", () => {
  it("scans a non-trivial number of files", () => {
    expect(sourceFiles.length).toBeGreaterThan(20);
  });

  for (const needle of FORBIDDEN) {
    it(`no source file contains ${needle}`, () => {
      const hits = [];
      for (const file of sourceFiles) {
        const text = fs.readFileSync(file, "utf8");
        text.split("\n").forEach((line, i) => {
          if (line.includes(needle)) {
            hits.push(`${path.relative(srcDir, file)}:${i + 1}: ${line.trim()}`);
          }
        });
      }
      expect(hits, hits.join("\n")).toEqual([]);
    });
  }

  it("uses the latch wording instead", () => {
    const appJsx = fs.readFileSync(path.join(srcDir, "app.jsx"), "utf8");
    expect(appJsx).toMatch(/密等鎖定/);
    expect(appJsx).toMatch(/latch/);
  });
});

describe("changelog 解凍(W1-9)", () => {
  it("is no longer frozen at 2026-06-12", () => {
    expect(CHANGELOG_VERSION).not.toBe("2026-06-12");
  });

  it("newest entry matches CHANGELOG_VERSION", () => {
    expect(CHANGELOG[0].version).toBe(CHANGELOG_VERSION);
  });

  it("has at least four more entries than the frozen version", () => {
    // 凍結時 = 2 條(2026-06-12 / 2026-06-11)。
    expect(CHANGELOG.length).toBeGreaterThanOrEqual(6);
  });

  it("is sorted newest first", () => {
    const versions = CHANGELOG.map((e) => e.version);
    expect([...versions].sort().reverse()).toEqual(versions);
  });

  it("every entry has a title and at least one item", () => {
    for (const entry of CHANGELOG) {
      expect(entry.title, JSON.stringify(entry)).toBeTruthy();
      expect(entry.items.length, entry.version).toBeGreaterThan(0);
    }
  });

  it("records the wording correction so users learn the term changed", () => {
    const flat = CHANGELOG.flatMap((e) => e.items).join("\n");
    expect(flat).toMatch(/密等鎖定/);
  });
});

describe("快捷鍵可發現性(W1-9 ②)", () => {
  it("composer placeholder tells the user how to open the shortcut panel", () => {
    const appJsx = fs.readFileSync(path.join(srcDir, "app.jsx"), "utf8");
    const placeholderLine = appJsx
      .split("\n")
      .find((line) => line.includes("placeholder=") && line.includes("問 ANILA"));
    expect(placeholderLine, "composer placeholder 不見了").toBeTruthy();
    expect(placeholderLine).toMatch(/⌘\/|Cmd\+\//);
  });
});

// ── 接線守門 ────────────────────────────────────────────────────────────────
// 元件本身的行為由 help.test.jsx / firstRun.test.jsx / memoryTab.test.jsx 覆蓋;
// 這裡只證明它們**真的被掛進 app.jsx**(元件測綠但沒接線 = 使用者看不到)。
// 之所以用源碼斷言而不是渲染整個 ChatRuntime:ChatRuntime 需要 auth context、
// 大量 fetch mock 與 mermaid/katex 的 jsdom 環境,那是另一個工作包(E2E)。
describe("app.jsx 接線(W1-9 / W1-10)", () => {
  const appJsx = fs.readFileSync(path.join(srcDir, "app.jsx"), "utf8");

  it("mounts the header help entry inside the Topbar", () => {
    const topbar = appJsx.slice(
      appJsx.indexOf("<Topbar>"),
      appJsx.indexOf("</Topbar>"),
    );
    expect(topbar.length).toBeGreaterThan(100);
    expect(topbar).toMatch(/<HelpButton\s+onOpen=/);
  });

  it("mounts the help panel and the ⌘/ hotkey", () => {
    expect(appJsx).toMatch(/<HelpPanel\s+open=\{helpOpen\}/);
    expect(appJsx).toMatch(/matchesHelpHotkey\(/);
  });

  it("mounts the first-run guide on the empty state", () => {
    expect(appJsx).toMatch(/<FirstRunGuide\s+onOpenHelp=/);
    const emptyBranch = appJsx.slice(
      appJsx.indexOf("currentMsgs.length === 0"),
      appJsx.indexOf("<EmptyState"),
    );
    expect(emptyBranch).toContain("FirstRunGuide");
  });

  it("passes the deployment capabilities into settings", () => {
    expect(appJsx).toMatch(/fetchCapabilities\(authRequest\)/);
    expect(appJsx).toMatch(/capabilities=\{capabilities\}/);
  });
});
