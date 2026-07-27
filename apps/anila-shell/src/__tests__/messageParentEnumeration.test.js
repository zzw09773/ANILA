// F4 —— 「未來的呼叫點不能靜悄悄加入錯的家族」。
//
// 前三條測試釘的是**目前**這幾個呼叫點的行為。但這個缺陷家族之所以會出現,不
// 是因為某個人寫錯,而是因為**什麼都不寫就會得到錯的語意**:新增一個建立訊息
// 的呼叫點、省略父節點,後端就默默把它掛到 active leaf。省略與「我要預設」在
// 程式碼裡長得一模一樣。
//
// 所以這裡把規則變成可執行的檢查:掃過整個前端原始碼,每一個建立訊息的呼叫點
// 要嘛**宣告父節點**,要嘛帶著理由出現在下面的 allowlist 上。第三種可能(什麼
// 都沒寫)直接讓這個檔案紅掉,而且錯誤訊息會指名 file:line。
//
// 另外驗線路本身的牙齒:`runtime/conversations.js` 的 appendMessage 對「沒宣告
// 父節點」是 throw,不是預設 —— 靜態檢查漏掉的話執行期還有一道。
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, it, expect } from "vitest";

import { appendMessage } from "../runtime/conversations.js";
import {
  IMPLICIT_ACTIVE_LEAF,
  MissingParentDeclarationError,
} from "../runtime/messageParent.js";

const SRC_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const APP_ROOT = path.resolve(SRC_ROOT, "..");

/**
 * 唯一合法的隱式用法。新增一筆之前先問:這個呼叫點真的是「使用者在現行分支上
 * 送出新的一輪提問」嗎?只要答案是「它其實想長一個兄弟」,就不該進這張表。
 */
const IMPLICIT_ALLOWLIST = [
  {
    file: "src/app.jsx",
    fn: "sendMessage",
    reason:
      "全新的使用者提問接在使用者眼前那條分支的尾端 —— active leaf 的定義正是那個節點。",
  },
];

/** 建立訊息的呼叫點長什麼樣。 */
const CREATION_PATTERNS = [
  // appendMessage(...) / apiAppendMessage(...) —— POST /messages 本體。
  { kind: "append", re: /(?<![A-Za-z0-9_$.])(?:api)?[Aa]ppendMessage\s*\(/g, needs: ["parentId"] },
  // createTurnPersistence({...}) —— 間接建立(user + assistant)。
  {
    kind: "turn",
    re: /(?<![A-Za-z0-9_$.])createTurnPersistence\s*\(/g,
    needs: ["userParentId", "assistantParentId"],
  },
];

function sourceFiles(dir) {
  const out = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (entry.name === "__tests__" || entry.name === "node_modules") continue;
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...sourceFiles(full));
    else if (/\.jsx?$/.test(entry.name)) out.push(full);
  }
  return out.sort();
}

/** 從 `(` 起算的括號內文字。會跳過字串/樣板字面值裡的括號。 */
function argumentText(source, openParenIdx) {
  let depth = 0;
  let quote = null;
  for (let i = openParenIdx; i < source.length; i++) {
    const ch = source[i];
    if (quote) {
      if (ch === "\\") i += 1;
      else if (ch === quote) quote = null;
      continue;
    }
    if (ch === '"' || ch === "'" || ch === "`") { quote = ch; continue; }
    if (ch === "(") depth += 1;
    else if (ch === ")") {
      depth -= 1;
      if (depth === 0) return source.slice(openParenIdx + 1, i);
    }
  }
  return source.slice(openParenIdx + 1);
}

/** 這個位置落在哪個具名 function 裡(往回找最近的宣告)。 */
function enclosingFunction(source, idx) {
  const re = /(?:async\s+)?function\s+([A-Za-z0-9_$]+)\s*\(/g;
  let name = "<top-level>";
  let m;
  while ((m = re.exec(source)) !== null) {
    if (m.index >= idx) break;
    name = m[1];
  }
  return name;
}

function lineOf(source, idx) {
  return source.slice(0, idx).split("\n").length;
}

/** 註解行上的字樣不是呼叫(模組開頭的用法示例會誤中)。 */
function isCommentLine(source, idx) {
  const lineStart = source.lastIndexOf("\n", idx) + 1;
  const head = source.slice(lineStart, idx).trimStart();
  return head.startsWith("//") || head.startsWith("*") || head.startsWith("/*");
}

/** 定義處(`function appendMessage(`)不是呼叫點。 */
function isDefinition(source, idx) {
  return /function\s+$/.test(source.slice(Math.max(0, idx - 20), idx));
}

function collectSites() {
  const sites = [];
  for (const file of sourceFiles(SRC_ROOT)) {
    const source = fs.readFileSync(file, "utf8");
    const rel = path.relative(APP_ROOT, file);
    for (const { kind, re, needs } of CREATION_PATTERNS) {
      re.lastIndex = 0;
      let m;
      while ((m = re.exec(source)) !== null) {
        const idx = m.index;
        if (isCommentLine(source, idx) || isDefinition(source, idx)) continue;
        const openParen = idx + m[0].length - 1;
        const args = argumentText(source, openParen);
        sites.push({
          kind,
          file: rel,
          line: lineOf(source, idx),
          fn: enclosingFunction(source, idx),
          // 沒有物件字面值 = 這裡只是把別處組好的 payload 轉手(例如
          // `appendMessage: (body) => apiAppendMessage(authRequest, convId, body)`)。
          // 宣告在上游那個組 payload 的呼叫點,而它自己也在這份掃描裡;真的漏
          // 了就由線路層的 throw 接住(見下面那組測試)。
          passthrough: !args.includes("{"),
          declares: needs.some((n) => new RegExp(`\\b${n}\\b`).test(args)),
          implicit: /\bIMPLICIT_ACTIVE_LEAF\b/.test(args),
          // 理由可以寫在呼叫點上方,也可以寫在物件字面值裡那個欄位旁邊。
          rationale: /message-parent:\s*implicit-active-leaf/.test(
            source.slice(Math.max(0, idx - 1200), idx) + args,
          ),
        });
      }
    }
  }
  return sites;
}

describe("F4 建立訊息的呼叫點列舉", () => {
  const sites = collectSites();

  it("掃得到東西(掃描器本身沒壞掉,不是空集合過關)", () => {
    expect(sites.length).toBeGreaterThanOrEqual(5);
    expect(sites.some((s) => s.file === "src/app.jsx")).toBe(true);
    expect(sites.some((s) => s.file === "src/runtime/streamPersistence.js")).toBe(true);
  });

  it("每個呼叫點都宣告父節點 —— 沒有一個是「什麼都不寫」", () => {
    const silent = sites
      .filter((s) => !s.declares && !s.passthrough)
      .map((s) => `${s.file}:${s.line}(${s.fn})`);
    expect(
      silent,
      "這些呼叫點建立訊息卻沒宣告父節點,伺服器會把它掛到 active leaf。" +
        "請指名父節點,或改用 fork/edit 端點,或(確定是全新一輪提問時)" +
        "顯式寫 IMPLICIT_ACTIVE_LEAF 並加進 IMPLICIT_ALLOWLIST。",
    ).toEqual([]);
  });

  it("用隱式預設的呼叫點就是 allowlist 上那些,一個不多一個不少", () => {
    const implicit = sites
      .filter((s) => s.implicit)
      .map((s) => ({ file: s.file, fn: s.fn }));
    expect(implicit).toEqual(
      IMPLICIT_ALLOWLIST.map(({ file, fn }) => ({ file, fn })),
    );
  });

  it("每個隱式呼叫點都在原地寫下理由(讀碼的人分得出兩種情況)", () => {
    const undocumented = sites
      .filter((s) => s.implicit && !s.rationale)
      .map((s) => `${s.file}:${s.line}(${s.fn})`);
    expect(
      undocumented,
      "隱式用法必須在呼叫點上方寫 `// message-parent: implicit-active-leaf —— 理由`。",
    ).toEqual([]);
  });

  it("allowlist 的每一筆都寫了理由", () => {
    for (const entry of IMPLICIT_ALLOWLIST) {
      expect(entry.reason.length, `${entry.file} ${entry.fn} 缺理由`).toBeGreaterThan(10);
    }
  });
});

describe("線路層的牙齒:appendMessage 對未宣告的父節點是 throw,不是預設", () => {
  const authRequest = () => Promise.resolve({});
  const body = (payload) => {
    let captured = null;
    appendMessage((_p, opts) => {
      captured = JSON.parse(opts.body);
      return Promise.resolve({});
    }, 1, payload);
    return captured;
  };

  it("省略 parentId → MissingParentDeclarationError", () => {
    expect(() => appendMessage(authRequest, 1, { role: "user", content: "x" })).toThrow(
      MissingParentDeclarationError,
    );
  });

  it("parentId: null 一樣 throw(null 就是「我忘了」的長相)", () => {
    expect(() =>
      appendMessage(authRequest, 1, { role: "user", content: "x", parentId: null }),
    ).toThrow(MissingParentDeclarationError);
  });

  it("IMPLICIT_ACTIVE_LEAF → 線上送 parent_id: null(讓伺服器接 active leaf)", () => {
    expect(body({ role: "user", content: "x", parentId: IMPLICIT_ACTIVE_LEAF }).parent_id)
      .toBeNull();
  });

  it("數字 → 原樣送上線", () => {
    expect(body({ role: "assistant", content: "x", parentId: 7 }).parent_id).toBe(7);
  });
});
