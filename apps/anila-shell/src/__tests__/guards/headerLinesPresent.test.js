// @source-text-guard
//
// 強度:**第三級(原始碼字串比對)**。它只擋「整段被刪掉」,擋不住
// 「還在但沒有被執行到」。行為級的覆蓋在 `transportHeaders.test.jsx`
// 與 `transportSessionAnswer.test.js`。
//
// 為什麼還是要有它:double-submit CSRF 的組裝在 shell 裡被抄了**五份**
// (runtime/api.js 的 buildHeaders 與 authMultipart、runtime/sse.js 的
// streamChatCompletion 與 streamSessionAnswer、runtime/tasks.js 的
// createTaskForConversation)。重複本身就是它最可能的死法 —— 有人做
// 「去重」或搬檔案時刪掉其中一份,而那一份剛好是當下沒有行為測試蓋到的。
// 這種整段消失是字串比對唯一真正擅長的事。
//
// ⚠ 這個檔案刻意放在子目錄,而且刻意用 `fs/promises` 的非同步讀檔 ——
// 2026-08-05 之前的登記簿(`sourceTextGuardRegistry.test.js`)兩者都看不到:
// 它的偵測只認同步版那一個函式名,而且目錄掃描沒有遞迴。也就是說這個檔案
// 在那時候可以完全不標記地混進來。兩個漏洞都已補上,這個檔案就是那個證明
// (證明方式見 README〈登記簿的兩個逃逸口〉)。

import { describe, it, expect } from "vitest";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";

const RUNTIME = resolve(process.cwd(), "src/runtime");

async function source(file) {
  return readFile(resolve(RUNTIME, file), "utf8");
}

/** 每一份自組標頭的路徑,和它一定要留著的字串。 */
const CSRF_COPIES = [
  {
    file: "api.js",
    what: "buildHeaders / authMultipart —— 所有控制面請求",
    needles: ["readCsrfCookie", "X-CSRF-Token"],
  },
  {
    file: "sse.js",
    what: "streamChatCompletion / streamSessionAnswer —— 兩條串流路徑",
    needles: ["anila_csrf", "X-CSRF-Token"],
  },
  {
    file: "tasks.js",
    what: "createTaskForConversation —— 失敗是靜默降級,最不容易被發現",
    needles: ["readCsrfCookie", "X-CSRF-Token"],
  },
];

describe("transport 標頭的原始碼守衛(第三級)", () => {
  for (const copy of CSRF_COPIES) {
    it(`runtime/${copy.file} 仍然自己組 CSRF 標頭(${copy.what})`, async () => {
      const body = await source(copy.file);
      for (const needle of copy.needles) {
        expect(body, `runtime/${copy.file} 少了 ${needle}`).toContain(needle);
      }
    });
  }

  it("sse.js 的 CSRF 組裝是兩份,不是一份(少一份就代表有人刪錯了)", async () => {
    const body = await source("sse.js");
    const copies = body.split('headers["X-CSRF-Token"]').length - 1;
    expect(copies).toBe(2);
  });

  it("sse.js 仍然掛得上對話與 Task 歸屬標頭", async () => {
    const body = await source("sse.js");
    expect(body).toContain("X-ANILA-Conversation-Id");
    expect(body).toContain("X-ANILA-Task-Id");
  });
});
