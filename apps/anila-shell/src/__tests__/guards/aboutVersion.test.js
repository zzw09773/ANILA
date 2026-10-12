// @source-text-guard —— 讀原始碼的字串比對（第三級強度），第二條 it 才是行為：vitest 裡真的拿得到 define 注入的版本。
// 逐頁走查 2026-09-02：設定 → 關於 寫著「v0.2.0」，而這條線已重定義為 v1.0.0
// 。版本字只准有一個來源：package.json，由 vite define 注入。
import { readFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, it, expect } from "vitest";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, "../../..");
const read = (f) => readFileSync(resolve(ROOT, f), "utf8");

describe("關於頁的版本號", () => {
  it("設定 → 關於（SettingsModal.jsx）不再寫死版本字串，改用 __ANILA_VERSION__", () => {
    // 關於頁從 app.jsx 拆到 components/SettingsModal.jsx；兩個檔都不准寫死。
    const src = read("src/components/SettingsModal.jsx");
    expect(src).not.toMatch(/v0\.2\.0/);
    expect(src).toMatch(/__ANILA_VERSION__/);
    expect(read("src/app.jsx")).not.toMatch(/v0\.2\.0/);
  });
  it("vite.config 從 package.json 注入 __ANILA_VERSION__（build 與 vitest 同一份）", () => {
    const cfg = read("vite.config.js");
    expect(cfg).toMatch(/__ANILA_VERSION__/);
    expect(cfg).toMatch(/package\.json/);
    // 這個測試本身就在 vitest 裡，所以 define 生效時這個全域會等於 package.json 的版本
    const pkg = JSON.parse(read("package.json"));
    expect(typeof __ANILA_VERSION__).toBe("string");
    expect(__ANILA_VERSION__).toBe(pkg.version);
  });
});
