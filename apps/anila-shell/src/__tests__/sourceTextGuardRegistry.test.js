// 測試套件對自己的誠實度檢查。
//
// 「讀原始碼 + toContain」的測試不是沒有價值 —— 它擋得住整段被刪掉。
// 但它**不是行為覆蓋率**:被測的那行程式碼可以整條路徑都沒被執行到,
// 字串還在檔案裡,測試照樣綠。稽核已經證實過:把對話歷史組裝弄壞,
// 全套 413 條(含這些字串比對)一條都沒紅。
//
// 所以規則是:任何用 readFileSync 讀原始碼來斷言的測試檔,
// 必須在檔頭標上 `@source-text-guard`,讓下一個人一眼看出它的強度。
// 新增一個沒標的,這條會紅。
//
// ⚠ 這條規則不是要淘汰那些測試 —— 不准為了讓它變綠就把它們刪掉。

import { describe, it, expect } from "vitest";
import { readdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";

const TEST_DIR = resolve(process.cwd(), "src/__tests__");
const MARKER = "@source-text-guard";

/** 已知會讀原始碼做字串比對的測試檔。新增請連同檔頭標記一起加。 */
const KNOWN_SOURCE_TEXT_GUARDS = [
  "composerFileAccept.test.js",
  "dupReplyReconcile.test.js",
  "editResend.test.js",
  "uxCopy.test.jsx",
];

function testFiles() {
  return readdirSync(TEST_DIR)
    .filter((f) => /\.test\.(js|jsx|mjs)$/.test(f))
    // 本檔自己會提到 readFileSync,不算在內。
    .filter((f) => f !== "sourceTextGuardRegistry.test.js");
}

function readsSourceText(file) {
  const body = readFileSync(resolve(TEST_DIR, file), "utf8");
  return body.includes("readFileSync");
}

describe("原始碼字串比對測試的登記簿", () => {
  it("每一個讀原始碼的測試檔都標了 @source-text-guard", () => {
    const unmarked = testFiles()
      .filter(readsSourceText)
      .filter((f) => !readFileSync(resolve(TEST_DIR, f), "utf8").includes(MARKER));
    expect(unmarked).toEqual([]);
  });

  it("登記簿與實際情況一致(多了或少了都要有人看一眼)", () => {
    const actual = testFiles().filter(readsSourceText).sort();
    expect(actual).toEqual([...KNOWN_SOURCE_TEXT_GUARDS].sort());
  });

  it("orchestrator 行為測試不靠讀原始碼過關", () => {
    const orchestratorFiles = testFiles().filter((f) => f.startsWith("orchestrator"));
    expect(orchestratorFiles.length).toBeGreaterThan(0);
    for (const f of orchestratorFiles) {
      expect(readsSourceText(f)).toBe(false);
    }
  });
});
