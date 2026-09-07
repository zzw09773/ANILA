// @source-text-guard
//
// 強度:**第三級(原始碼字串比對)**。頂欄「專案入口」已拿掉，側欄彈窗才是入口。
// 行為測試蓋不到「有人又加回 IconButton」。
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

describe("chat header chrome", () => {
  it("does not duplicate 專案入口 next to 設定", () => {
    const src = readFileSync(resolve(process.cwd(), "src/app.jsx"), "utf8");
    expect(src).not.toMatch(/IconButton title="專案入口"/);
  });
});
