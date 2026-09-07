// 測試套件對自己的誠實度檢查。
//
// 「讀原始碼 + toContain」的測試不是沒有價值 —— 它擋得住整段被刪掉。
// 但它**不是行為覆蓋率**:被測的那行程式碼可以整條路徑都沒被執行到,
// 字串還在檔案裡,測試照樣綠。稽核已經證實過:把對話歷史組裝弄壞,
// 全套 413 條(含這些字串比對)一條都沒紅。
//
// 所以規則是:任何讀原始碼來斷言的測試檔,必須在檔頭標上
// `@source-text-guard`,讓下一個人一眼看出它的強度。新增一個沒標的,這條會紅。
//
// ⚠ 這條規則不是要淘汰那些測試 —— 不准為了讓它變綠就把它們刪掉。
//
// ── 兩個已知界線,寫在這裡是因為下一個人會需要 ──────────────────────
//
// 1. **標記是檔案級的,不是測試級的。** 一個檔案只要有一條讀原始碼,
//    整個檔案就被標成第三級 —— 也就是說它標的是**該檔最弱的那條測試**。
//    目前被標的四個檔都是純字串比對檔,沒有東西被誤標;但如果哪天有人
//    在一個行為測試檔裡塞一條字串比對,整檔會被降級標示,而那個標示會
//    低估同檔其他測試的強度。要避免的話:**把字串比對放在自己的檔案裡**
//    (慣例是 `src/__tests__/guards/`),不要和行為測試混在同一檔。
//
// 2. **偵測是黑名單,黑名單永遠補不完。** 下面 `SOURCE_READ_SIGNALS` 列的
//    是已知的讀檔手法;`eval` 一段組出來的字串、把讀檔包進 helper 再 import、
//    或任何還沒被想到的寫法都繞得過去。真正的防線不是這份清單,而是
//    `KNOWN_SOURCE_TEXT_GUARDS` 要求**完全相符**:任何新增/移除都會讓這裡
//    變紅,逼人動手改這個清單,而那個改動一定出現在 diff 裡讓審查者看到。
//    清單只是把「無意間繞過」(換個習慣用 fs/promises)擋掉;
//    「刻意繞過」擋不住,也不打算擋。

import { describe, it, expect } from "vitest";
import { readdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";

const TEST_DIR = resolve(process.cwd(), "src/__tests__");
const MARKER = "@source-text-guard";

/**
 * 已知會讀原始碼做字串比對的測試檔(相對於 `src/__tests__/`)。
 * 新增請連同檔頭標記一起加。
 */
const KNOWN_SOURCE_TEXT_GUARDS = [
  "composerFileAccept.test.js",
  "dupReplyReconcile.test.js",
  "editResend.test.js",
  "guards/aboutVersion.test.js",
  "guards/composerNarrowHint.test.js",
  "guards/headerLinesPresent.test.js",
  "guards/noProjectEntryButton.test.js",
  "mermaidVersionGuard.test.js",
  "uxCopy.test.jsx",
];

/**
 * 讀檔的訊號。同步/非同步/串流三種都要看 —— 只看 `readFileSync` 的話,
 * 一個習慣用 `fs/promises` 的人寫出來的字串比對測試會完全隱形
 * (`guards/headerLinesPresent.test.js` 就是那個形狀)。
 */
const SOURCE_READ_SIGNALS = [
  "readFileSync",
  "readFile(", // fs/promises 或 callback 版
  "createReadStream",
  "openSync",
  // 動態載入 fs 之後再取用,靜態 import 掃不到。
  'import("fs',
  'import("node:fs',
  'require("fs',
  'require("node:fs',
];

/** 遞迴列出所有測試檔(相對路徑)。 */
function testFiles() {
  return readdirSync(TEST_DIR, { recursive: true })
    .map((f) => String(f).split("\\").join("/"))
    .filter((f) => /\.test\.(js|jsx|mjs)$/.test(f))
    // 本檔自己會提到那些訊號字串,不算在內。
    .filter((f) => f !== "sourceTextGuardRegistry.test.js");
}

function bodyOf(file) {
  return readFileSync(resolve(TEST_DIR, file), "utf8");
}

/**
 * 拿掉註解再偵測。
 *
 * 不做這件事的話,一個**在註解裡說「這裡不用讀原始碼」的行為測試檔**
 * 會被判成第三級 —— `wt/shell-reserve` 的 `reservedTurn.test.jsx` 正是
 * 這個形狀(第 5 行的說明文字提到那個函式名)。誤判的方向雖然是安全的
 * (寧可多標),但它會逼下一個人去讀一個 1770 行的檔案找不存在的問題,
 * 而「花時間查一個假警報」正是讓人開始忽略整組守衛的起點。
 *
 * 只用在偵測;`@source-text-guard` 標記本身在註解裡,查標記時不能先剝。
 */
function stripComments(source) {
  return source
    .replace(/\/\*[\s\S]*?\*\//g, " ")
    .replace(/(^|[^:])\/\/[^\n]*/g, "$1");
}

function readsSourceText(file) {
  const code = stripComments(bodyOf(file));
  return SOURCE_READ_SIGNALS.some((signal) => code.includes(signal));
}

/** 行為/單元測試(第一、二級)—— 這些不准靠讀原始碼過關。 */
function behaviouralFiles() {
  return testFiles().filter((f) => /^(orchestrator|transport)/.test(f));
}

describe("原始碼字串比對測試的登記簿", () => {
  it("每一個讀原始碼的測試檔都標了 @source-text-guard", () => {
    const unmarked = testFiles()
      .filter(readsSourceText)
      .filter((f) => !bodyOf(f).includes(MARKER));
    expect(unmarked).toEqual([]);
  });

  it("登記簿與實際情況一致(多了或少了都要有人看一眼)", () => {
    const actual = testFiles().filter(readsSourceText).sort();
    expect(actual).toEqual([...KNOWN_SOURCE_TEXT_GUARDS].sort());
  });

  it("行為測試不靠讀原始碼過關", () => {
    const files = behaviouralFiles();
    expect(files.length).toBeGreaterThan(0);
    for (const f of files) {
      expect(readsSourceText(f), `${f} 在讀原始碼`).toBe(false);
    }
  });

  it("註解裡提到讀檔函式不算(不然行為測試會被誤標成第三級)", () => {
    const behavioural = [
      "// 這裡不用 readFileSync + toContain 檢查原始碼文字 —— 一律掛起來測。",
      "/* createReadStream 也一樣不用 */",
      'it("真的做事", () => { expect(1).toBe(1); });',
    ].join("\n");
    expect(SOURCE_READ_SIGNALS.some((s) => stripComments(behavioural).includes(s))).toBe(
      false,
    );

    // 但真的在程式碼裡讀檔就一定要抓到 —— 剝註解不能把偵測剝掉。
    const guardShaped = [
      "// 說明文字",
      'const body = await readFile(resolve(RUNTIME, "api.js"), "utf8");',
    ].join("\n");
    expect(SOURCE_READ_SIGNALS.some((s) => stripComments(guardShaped).includes(s))).toBe(
      true,
    );
  });

  it("掃描是遞迴的(子目錄裡的字串比對測試不會隱形)", () => {
    // 這一條釘住的是掃描方式本身。曾經 `readdirSync` 沒有 recursive,
    // 於是 `guards/` 底下的檔案完全不在視野內 —— 規則看起來在跑,
    // 實際上有一整個目錄不受管。
    const scanned = testFiles();
    expect(scanned.some((f) => f.includes("/"))).toBe(true);
    expect(scanned).toContain("guards/headerLinesPresent.test.js");
  });
});
