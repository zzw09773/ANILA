#!/usr/bin/env node
// 突變檢查 — 證明測試真的抓得到產品壞掉。
//
// 用法(cwd = apps/anila-shell):
//   node scripts/mutation-check.mjs            # 全部突變
//   node scripts/mutation-check.mjs history-*  # 只跑符合的
//   node scripts/mutation-check.mjs --list
//
// 每一個突變都做同一件事:
//   1. 確認乾淨狀態下目標測試是綠的
//   2. 把 `find` 換成 `replace`(必須剛好命中一次,否則報錯離開)
//   3. 跑**新的 orchestrator 測試**與**改動前就存在的測試**兩組
//   4. 還原檔案,再確認回到綠
//
// 設計約束:每個突變都**保留所有識別字**。改的是運算子、索引、
// 屬性名這種東西,所以「grep 原始碼有沒有這個字」的測試救不了你。
// 至少要有一個是「刪掉一個賦值 / 讓存取器回空值」那一型 —— 那正是
// 既有測試全綠、產品卻送出零歷史的那一型。
//
// 離開碼:任何一個突變存活(該紅卻沒紅)= 1。

import { execFileSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

const ROOT = process.cwd();

// 新寫的 orchestrator 行為測試。
const NEW_TESTS = ["src/__tests__/orchestrator"];
// 這個工作包之前就存在的測試(用來量「舊套件漏了什麼」)。
const PRE_EXISTING_EXCLUDES = [
  "**/node_modules/**",
  "**/*.node.test.mjs",
  "**/orchestrator*.test.jsx",
  "**/sourceTextGuardRegistry.test.js",
];

/**
 * @type {{id:string,file:string,find:string,replace:string,intent:string,shape?:string}[]}
 */
const MUTATIONS = [
  {
    id: "history-prior-empty",
    file: "src/app.jsx",
    shape: "存取器回空值",
    intent: "送出時的對話歷史永遠是空的（模型看不到前文）",
    find: "    const priorForHistory = messagesByConv[convId] || [];",
    replace: "    const priorForHistory = messagesByConv[convId] && [];",
  },
  {
    id: "history-loop-skipped",
    file: "src/app.jsx",
    shape: "存取器回空值",
    intent: "歷史組裝的迴圈永遠跑零圈，只送當前這一句",
    find: "  for (const m of priorMsgs || []) {",
    replace: "  for (const m of priorMsgs && []) {",
  },
  {
    id: "history-drops-assistant",
    file: "src/app.jsx",
    shape: "條件反轉",
    intent: "歷史只留使用者的話，助理答過什麼全部丟掉",
    find: '    } else if (m.role === "assistant" && m.text) {',
    replace: '    } else if (m.role === "assistant" && !m.text) {',
  },
  {
    id: "update-msg-wipes-list",
    file: "src/app.jsx",
    shape: "存取器回空值",
    intent: "訊息更新時把整格對話清空（畫面上的字會消失）",
    // 用 updateMsg 的函式簽章當錨點 —— 光是那一行賦值在 app.jsx 裡有兩處。
    find:
      "  function updateMsg(convId, msgId, patch) {\n" +
      "    setMessagesByConv((prev) => {\n" +
      "      const list = prev[convId] || [];",
    replace:
      "  function updateMsg(convId, msgId, patch) {\n" +
      "    setMessagesByConv((prev) => {\n" +
      "      const list = prev[convId] && [];",
  },
  {
    id: "conversation-switch-noop",
    file: "src/app.jsx",
    shape: "控制項變成靜默 no-op",
    intent: "點側邊欄的對話沒有反應（按了、沒報錯、什麼也沒發生）",
    find: "          setSelectedConvId(id);",
    replace: "          setSelectedConvId(selectedConvId);",
  },
  {
    id: "assistant-never-persisted",
    file: "src/app.jsx",
    shape: "刪掉整段存檔（靜默失敗）",
    intent: "助理回答完全沒有存進後端，畫面正常、重新整理就不見了",
    // appendAssistant 開頭的守衛反轉 = 存檔整段被跳過，而且沒有任何錯誤訊息。
    find:
      "          // Persist the assistant turn under the user message's db id.\n" +
      '          if (typeof convId !== "number") return;',
    replace:
      "          // Persist the assistant turn under the user message's db id.\n" +
      '          if (typeof convId === "number") return;',
  },
  {
    id: "persist-error-not-pinned",
    file: "src/app.jsx",
    shape: "刪掉標記（靜默失敗）",
    intent: "存檔失敗時氣泡上不再標記（使用者以為存好了）",
    find: "            updateMsg(convId, assistantId, { persistError: persisted.notice });",
    replace: "            updateMsg(convId, assistantId, { persistError: persisted.saved });",
  },
  {
    id: "persist-2xx-without-id-accepted",
    file: "src/runtime/messageTree.js",
    shape: "條件放寬",
    intent: "後端回 2xx 但沒有 id 也算存檔成功（最典型的靜默成功）",
    // 同一行守衛在 messageTree.js 出現三次，用函式簽章鎖定 reconcile 那一個。
    find:
      "export function reconcilePersistedAssistant(saved, fallbackParentId = null) {\n" +
      "  const missNotice = PERSIST_MISS_NOTICE;\n" +
      '  if (saved && typeof saved.id === "number") {',
    replace:
      "export function reconcilePersistedAssistant(saved, fallbackParentId = null) {\n" +
      "  const missNotice = PERSIST_MISS_NOTICE;\n" +
      '  if (saved || typeof saved.id === "number") {',
  },
  {
    id: "persist-turn-2xx-without-id-accepted",
    file: "src/runtime/messageTree.js",
    shape: "條件放寬",
    intent: "送出路徑上，2xx-without-id 被當成存檔成功",
    find:
      "    const saved = await appendMessage(authRequest, convId, payload);\n" +
      '    if (saved && typeof saved.id === "number") {',
    replace:
      "    const saved = await appendMessage(authRequest, convId, payload);\n" +
      '    if (saved || typeof saved.id === "number") {',
  },
  {
    id: "backend-error-text-swallowed",
    file: "src/runtime/sse.js",
    shape: "訊息被吃掉",
    intent: "後端說的失敗原因被換成通用字串（使用者不知道發生什麼事）",
    find: '    const error = new Error(detail || "Streaming failed");',
    replace: '    const error = new Error(detail && "Streaming failed");',
  },
  {
    id: "agents-load-error-hidden",
    file: "src/app.jsx",
    shape: "訊息被吃掉",
    intent: "agent 清單載入失敗不再顯示 banner（選單空白但沒人說為什麼）",
    find: '        setRuntimeError(error.message || "無法載入 agent 清單");',
    replace: '        setRuntimeError(error.message && "");',
  },
  {
    id: "edit-history-not-truncated",
    file: "src/app.jsx",
    shape: "少了截斷",
    intent: "編輯重問時舊問句沒被切掉，同一題送兩次給模型",
    find: "    const historyPrior = existing.slice(0, idx);",
    replace: "    const historyPrior = existing;",
  },
  {
    id: "regenerate-history-wrong-cut",
    file: "src/app.jsx",
    shape: "取錯索引",
    intent: "重新產生時把要重問的那一題也塞進歷史",
    find:
      "      messages: buildMessageHistory(msgs.slice(0, userIdx), steeredUserText, prevUser.attachments || []),",
    replace:
      "      messages: buildMessageHistory(msgs.slice(0, idx), steeredUserText, prevUser.attachments || []),",
  },
  {
    id: "focus-refresh-throttle-removed",
    file: "src/app.jsx",
    shape: "節流失效",
    intent: "每次切回視窗都重抓 agent 清單（共用工作站上會把 CSP 打爆）",
    find: "      if (now - lastAgentsRefreshAtRef.current < 15_000) return;",
    replace: "      if (now - lastAgentsRefreshAtRef.current > 15_000) return;",
  },
  {
    id: "stop-button-does-nothing",
    file: "src/app.jsx",
    shape: "控制項變成靜默 no-op",
    intent: "「停止產生」按了沒有反應（串流照樣跑完）",
    // 註:改成 `get(selectedConvId)` 是**等價突變** —— stopStreaming 的每一個
    // 可達呼叫端傳進來的都已經是當前選取的對話，換掉不改變任何行為。
    // 真正會壞的是 abort 本身沒被呼叫，所以守衛反轉才是有效的突變。
    find: "    if (controller) controller.abort();",
    replace: "    if (!controller) controller.abort();",
  },
  {
    id: "regenerate-not-branched",
    file: "src/app.jsx",
    shape: "條件反轉",
    intent: "重新產生不再開兄弟節點（舊答案被擠掉，分支歷史消失）",
    find: '          if (typeof assistantMsg.dbId === "number") {',
    replace: '          if (typeof assistantMsg.dbId === "string") {',
  },
];

// ---- 執行 ------------------------------------------------------------------

function runVitest(args) {
  try {
    const out = execFileSync("npx", ["vitest", "run", ...args], {
      cwd: ROOT,
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
      env: { ...process.env, CI: "1" },
    });
    return { green: true, out };
  } catch (err) {
    return { green: false, out: `${err.stdout || ""}${err.stderr || ""}` };
  }
}

const runNew = () => runVitest(NEW_TESTS);
const runPreExisting = () =>
  runVitest(PRE_EXISTING_EXCLUDES.flatMap((e) => ["--exclude", e]));

function summarise(out) {
  const m = out.match(/Tests\s+(.+)/);
  return m ? m[1].trim() : "（無法解析）";
}

function applyMutation(mut) {
  const path = resolve(ROOT, mut.file);
  const original = readFileSync(path, "utf8");
  const hits = original.split(mut.find).length - 1;
  if (hits !== 1) {
    throw new Error(
      `突變 ${mut.id}: 在 ${mut.file} 找到 ${hits} 處符合，需要剛好 1 處。` +
        `原始碼可能已改動，請更新 scripts/mutation-check.mjs。`,
    );
  }
  writeFileSync(path, original.replace(mut.find, mut.replace), "utf8");
  return () => writeFileSync(path, original, "utf8");
}

function main() {
  const argv = process.argv.slice(2);
  if (argv.includes("--list")) {
    for (const m of MUTATIONS) console.log(`${m.id}\t${m.file}\t${m.intent}`);
    return 0;
  }
  const patterns = argv.filter((a) => !a.startsWith("--"));
  const selected = patterns.length
    ? MUTATIONS.filter((m) =>
        patterns.some((p) =>
          new RegExp(`^${p.replace(/[.+?^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*")}$`).test(m.id),
        ),
      )
    : MUTATIONS;

  if (selected.length === 0) {
    console.error("沒有符合的突變。用 --list 看清單。");
    return 2;
  }

  console.log("== 前置:確認未突變時是綠的 ==");
  const baseNew = runNew();
  if (!baseNew.green) {
    console.error("新測試在乾淨狀態下就是紅的，先修好再跑突變檢查。");
    console.error(baseNew.out.slice(-3000));
    return 2;
  }
  console.log(`  新 orchestrator 測試: ${summarise(baseNew.out)}`);
  const basePre = runPreExisting();
  console.log(`  既有測試:             ${summarise(basePre.out)}`);
  console.log("");

  const results = [];
  for (const mut of selected) {
    process.stdout.write(`-- ${mut.id} … `);
    let restore;
    try {
      restore = applyMutation(mut);
      const rNew = runNew();
      const rPre = runPreExisting();
      results.push({
        id: mut.id,
        file: mut.file,
        shape: mut.shape,
        intent: mut.intent,
        caughtByNew: !rNew.green,
        caughtByPreExisting: !rPre.green,
      });
      console.log(
        `新測試 ${!rNew.green ? "紅 ✓" : "綠 ✗（存活）"} / 既有測試 ${
          !rPre.green ? "紅" : "綠"
        }`,
      );
    } finally {
      if (restore) restore();
    }
    const after = runNew();
    if (!after.green) {
      console.error(`還原後 ${mut.id} 仍是紅的 — 工作目錄可能已污染，中止。`);
      return 2;
    }
  }

  console.log("\n== 突變表 ==");
  console.log(
    "| 突變 | 形狀 | 壞掉的行為 | 新測試抓到 | 既有測試抓到 |",
  );
  console.log("|---|---|---|---|---|");
  for (const r of results) {
    console.log(
      `| \`${r.id}\` | ${r.shape} | ${r.intent} | ${
        r.caughtByNew ? "是" : "**否**"
      } | ${r.caughtByPreExisting ? "是" : "否"} |`,
    );
  }

  const survivors = results.filter((r) => !r.caughtByNew);
  console.log("");
  console.log(
    `新測試抓到 ${results.length - survivors.length} / ${results.length}；` +
      `既有測試抓到 ${results.filter((r) => r.caughtByPreExisting).length} / ${results.length}。`,
  );
  if (survivors.length > 0) {
    console.log("存活的突變（測試沒抓到）:");
    for (const s of survivors) console.log(`  - ${s.id}: ${s.intent}`);
    return 1;
  }
  return 0;
}

process.exit(main());
