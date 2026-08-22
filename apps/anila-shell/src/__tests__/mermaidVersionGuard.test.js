/**
 * @source-text-guard
 *
 * Mermaid strict-mode was proven in a real browser on 11.16.1
 * (2026-08-21, 資安官, LEDGER.md §27). The caret on this dependency
 * would let an unrelated `npm install` slide mermaid forward and
 * silently drop that proof. This test is the trigger to re-run that
 * browser check — it does not replace it.
 *
 * mermaid.test.jsx still guards securityLevel:"strict" itself.
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const SHELL_ROOT = join(here, "../..");

/**
 * Last mermaid version whose securityLevel:"strict" was proven in a
 * real Chromium against live mermaid SVG in the DOM (2026-08-21,
 * 資安官真瀏覽器實測，LEDGER.md §27).
 *
 * 更新這個常數之前必須先重跑真瀏覽器實測並附紀錄；只改常數讓測試變綠，正是這條守衛要擋的動作。
 */
const LAST_BROWSER_VERIFIED_MERMAID_STRICT = "11.16.1";

function mermaidVersionFromLockfile() {
  const lock = JSON.parse(readFileSync(join(SHELL_ROOT, "package-lock.json"), "utf8"));
  const entry = lock.packages?.["node_modules/mermaid"];
  if (!entry || typeof entry.version !== "string") {
    throw new Error("package-lock.json 沒有 node_modules/mermaid.version");
  }
  return entry.version;
}

describe("mermaid version matches last browser-verified strict", () => {
  it("lockfile mermaid === LAST_BROWSER_VERIFIED_MERMAID_STRICT", () => {
    const locked = mermaidVersionFromLockfile();
    expect(
      locked,
      `mermaid 鎖檔是 ${locked}，與最後一次真瀏覽器驗過 strict 的版本 ${LAST_BROWSER_VERIFIED_MERMAID_STRICT} 不符。` +
        `要重跑那個真瀏覽器實測（資安官 2026-08-21／LEDGER.md §27：真 Chromium、活 DOM 插 SVG、敵意圖表），` +
        `附紀錄後才能更新 LAST_BROWSER_VERIFIED_MERMAID_STRICT。只改常數讓測試變綠，正是這條守衛要擋的動作。`,
    ).toBe(LAST_BROWSER_VERIFIED_MERMAID_STRICT);
  });
});
