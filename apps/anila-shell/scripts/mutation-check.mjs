#!/usr/bin/env node
// 突變檢查 — 證明測試真的抓得到產品壞掉。
//
// 用法(cwd = apps/anila-shell):
//   node scripts/mutation-check.mjs            # 全部突變
//   node scripts/mutation-check.mjs history-*  # 只跑符合的
//   node scripts/mutation-check.mjs --list
//   node scripts/mutation-check.mjs --check-anchors  # 只驗錨點,不跑測試
//   node scripts/mutation-check.mjs --restore        # 只修上一輪中止的殘留
//
// 每一個突變都做同一件事:
//   1. 確認乾淨狀態下**兩組**測試都是綠的
//   2. 把 `find` 換成 `replace`(必須剛好命中一次,否則報錯離開)
//   3. 跑**新寫的行為測試**與**改動前就存在的測試**兩組
//   4. 還原檔案,再確認**兩組**都回到綠 —— 這同時就是下一個突變的前置狀態
//
// 為什麼第 4 步要驗**兩組**而不只是新測試:整份報告最有價值的那個數字是
// 「既有測試只抓到 N 個」。如果某條既有測試在突變**之前**就已經是紅的,
// 「被突變殺掉」和「本來就壞著」在結果裡長得一模一樣,那個數字就沒有意義。
// 所以每一個突變都必須從一個**已知全綠**的狀態出發,而不是假設它是。
//
// 設計約束:每個突變都**保留所有識別字**。改的是運算子、索引、
// 屬性名這種東西,所以「grep 原始碼有沒有這個字」的測試救不了你。
// 至少要有一個是「刪掉一個賦值 / 讓存取器回空值」那一型 —— 那正是
// 既有測試全綠、產品卻送出零歷史的那一型。
//
// ── 無法從這個 harness 觸及的形狀(刻意留白,不是忘了)──────────────
//
// * `runtime/api.js` 的 `authMultipart`(附件上傳自己組的第三份 CSRF):
//   從掛起來的 shell 觸發需要走完檔案挑選 → 上傳的 UI 流程,jsdom 下的
//   FormData/File 行為和瀏覽器差太多,做出來的會是在測 harness。
//   已由 `wt/shell-reserve` 的 `csrfHeaders.test.js` 在函式層面收掉;
//   兩個分支合併後這一格才會有守衛。**在本分支單獨跑會存活**,所以
//   不列進清單 —— 列了就是報一個假的紅。
//   ⚠ 2026-08-05 兩個分支已經合併(263d4f46),`csrfHeaders.test.js` 在樹上了
//   (而且已歸進 NEW_TESTS,理由見下面),所以這一格**可以**補一個突變了。
//   這一輪沒補,是因為範圍只有重新錨定與修基準線;補的人請照
//   `csrf-missing-on-*` 那幾條的寫法。
// * `runtime/sse.js` 的 `streamSessionAnswer`:`app.jsx` 目前沒有任何
//   呼叫端(全樹 grep 只有定義與註解),掛起來的 shell 走不到。
//   改用直接呼叫的 `transportSessionAnswer.test.js` 收,突變照列。
//
// ── 合併 263d4f46 之後發生過的事(留著,因為它會再發生一次)─────────────
//
// 本包(`wt/test-foundation`)是從 reserve 改寫**之前**的 app.jsx 長出來的。
// `wt/shell-reserve` 把送出路徑換成「先落庫再串流」:使用者訊息與助理列由
// `POST /api/conversations/{id}/turn` 在同一個交易裡建好,串流結束再
// `PUT .../messages/{id}` 寫回。git 合併零衝突,因為兩包動的是不同的 hunk。
//
// 於是同一個原因造成了三種不同大聲程度的損壞:
//
//  1. **28 條測試紅**:`helpers/fakeBackend.js` 不認得 `/turn`,一律回 404 →
//     整輪在串流開始前就中止。已修(該檔補了三個端點,語意照抄
//     `fakeConversationBackend.js`);細節與注入點的搬移記在
//     `src/__tests__/README.md` 的〈合併漂移事故〉。
//  2. **4 個錨點漂掉**:舊錨點釘在被改寫掉的那幾行(其中一個還把註解一起
//     釘了進去)。已重新錨定,每一個都寫了為什麼釘在新的那一行。
//  3. **1 個錨點還在、但守的碼變成死碼**:`persist-turn-2xx-without-id-accepted`
//     原本釘 `messageTree.js` 的 `persistAssistantTurn`,而送出路徑已經不走
//     它了。**這一種最安靜** —— `--check-anchors` 全綠、既有測試照樣紅,
//     唯一的徵兆是它在「新測試」那一組存活。已改釘到 `reservedTurn.js`。
//
// 教訓:`--check-anchors` 通過**不代表**突變清單還對得上產品。它只證明那些
// 字串還在檔案裡,不證明產品還會跑到那裡。整輪跑完才會說出第 3 種。
//
// 離開碼:任何一個突變存活(該紅卻沒紅)= 1。

import { spawn } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";

const ROOT = process.cwd();

// 這個工作包新寫的行為測試(orchestrator 掛載 + transport 標頭)。
//
// `csrfHeaders.test.js` 是 `wt/shell-reserve` 寫的,不是本包寫的 —— 但它同樣是
// 2026-08-05 才落地的**新**測試,而且收的正是本清單 transport 那一區的同一格
// (見檔頭第 29 行起的說明)。把它留在「既有測試」那一桶,csrf-* 那幾個突變
// 就會顯示成「既有測試也抓到了」,而那個數字整份報告只有一個用途:量**舊套件**
// 漏了什麼。今天早上才寫的守衛不是舊套件。所以歸到這一桶。
const NEW_TESTS = [
  "src/__tests__/orchestrator",
  "src/__tests__/transport",
  "src/__tests__/csrfHeaders",
  "src/__tests__/agentRefreshFeedback",
  // 敏感資訊閘門與模式偏好(`wt/pii-honesty`,2026-08-05)。這一組守的是新加的
  // 那幾個突變 —— 連同它們釘的行為都是本包才長出來的,所以歸「新測試」。
  "src/__tests__/redactionBlockAttribution",
  "src/__tests__/redactionModePersistence",
  "src/__tests__/redactionGateCoverage",
  "src/__tests__/redactionChokePoint",
  "src/__tests__/settingsPrivacyHonesty",
  // 憑證偵測與四條既有 pattern 的精準度(`wt/credential-detect`,2026-08-06)。
  "src/__tests__/credentialDetect",
  "src/__tests__/agentReplySignal",
];
// 這個工作包之前就存在的測試(用來量「舊套件漏了什麼」)。
const PRE_EXISTING_EXCLUDES = [
  "**/node_modules/**",
  "**/*.node.test.mjs",
  "**/orchestrator*.test.jsx",
  "**/transport*.test.*",
  "**/csrfHeaders.test.js",
  "**/agentRefreshFeedback.test.jsx",
  "**/redactionBlockAttribution.test.jsx",
  "**/redactionModePersistence.test.jsx",
  "**/redactionGateCoverage.test.jsx",
  "**/redactionChokePoint.test.jsx",
  "**/settingsPrivacyHonesty.test.jsx",
  "**/credentialDetect.test.jsx",
  "**/agentReplySignal.test.jsx",
  "**/__tests__/guards/**",
  "**/sourceTextGuardRegistry.test.js",
];

/**
 * @type {{id:string,file:string,find:string,replace:string,intent:string,shape?:string}[]}
 */
const MUTATIONS = [
  {
    id: "agent-short-reply-notice-inverted",
    file: "src/runtime/agentReplySignal.js",
    shape: "條件反轉",
    intent: "觀察到極短 agent 回覆時不再顯示平台觀測提示",
    find: "  if (observation.short_reply !== true) return null;",
    replace: "  if (observation.short_reply === true) return null;",
  },
  {
    id: "history-prior-empty",
    file: "src/app.jsx",
    shape: "存取器回空值",
    intent: "送出時的對話歷史永遠是空的（模型看不到前文）",
    // 2026-08-05 重新錨定(reserve-then-stream 落地後)。舊錨點是
    // `const priorForHistory = messagesByConv[convId] || [];` —— 歷史來源改成
    // `historyBefore(messagesRef.current[convId] || [], userMsg.id)` 之後整行消失。
    // 缺陷本身完全沒有消失:歷史照樣可能送成空的。新錨點釘在**送給模型的
    // payload 那一行** —— 不管歷史怎麼組、組在哪裡,它都得從這個交界出去,
    // 下一次重寫搬得動組裝方式,搬不掉這一行。
    find: "        messages: buildMessageHistory(priorForHistory, text, attachments),",
    replace: "        messages: buildMessageHistory(priorForHistory && [], text, attachments),",
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
    shape: "清單順序被反轉",
    intent: "訊息更新時把整段對話順序反轉（畫面與歷史順序被打亂）",
    // 用 updateMsg 的函式簽章當錨點 —— 光是那一行賦值在 app.jsx 裡有兩處。
    find:
      "  function updateMsg(convId, msgId, patch) {\n" +
      "    setMessagesByConv((prev) => {\n" +
      "      const list = prev[convId] || [];",
    replace:
      "  function updateMsg(convId, msgId, patch) {\n" +
      "    setMessagesByConv((prev) => {\n" +
      "      const list = [...(prev[convId] || [])].reverse();",
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
    // 2026-08-05 重新錨定。舊錨點是 appendAssistant 開頭的守衛(連同它上面
    // 那行英文註解)—— 助理列改成 POST /turn 先預留、串流結束才 PUT 寫回之後,
    // append 那條路整段不存在了。⚠ 舊錨點把註解也一起釘進去,那正是它為什麼
    // 這麼容易漂掉:註解是最不 load-bearing 的東西。
    //
    // 缺陷仍然成立,而且比以前更安靜:寫回那一段被跳過的話,那一列會永遠停在
    // reserved 且內容是空的,畫面上答案好端端地顯示著(text 在上面幾行就已經
    // updateMsg 進去了),重新整理才發現不見了。新錨點是寫回段的前置守衛,
    // 與舊突變同型 —— 前置守衛反轉 = 整段存檔靜悄悄地跳過,零錯誤訊息。
    find: "      if (!persistable || reservedId == null) return;",
    replace: "      if (persistable || reservedId == null) return;",
  },
  {
    id: "persist-error-not-pinned",
    file: "src/app.jsx",
    shape: "刪掉標記（靜默失敗）",
    intent: "存檔失敗時氣泡上不再標記（使用者以為存好了）",
    // 2026-08-05 重新錨定。突變本身與舊的一字不差(notice → saved;存檔失敗時
    // saved 是 null,氣泡就不會被標記,而全域 banner 照樣出現 —— 使用者看到的
    // 是「有個東西閃過去了，但這則回答看起來好好的」)。漂掉的原因只是縮排:
    // 這一段從 appendAssistant 的深層 callback 搬到了串流鏈的頂層。
    //
    // 它現在在 app.jsx 出現**兩次**(送出路徑與編輯重問路徑),所以多帶一個
    // 右大括號把它鎖在送出路徑上 —— 編輯重問那一份的下一行是 `return;`。
    find:
      "        updateMsg(convId, assistantId, { persistError: persisted.notice });\n" +
      "      }",
    replace:
      "        updateMsg(convId, assistantId, { persistError: persisted.saved });\n" +
      "      }",
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
    file: "src/runtime/reservedTurn.js",
    shape: "條件放寬",
    intent: "送出路徑上，2xx-without-id 被當成存檔成功",
    // 2026-08-05 重新錨定,而且理由跟另外四個不一樣 —— 這一個的舊錨點**還在**,
    // 只是它守的那個函式已經沒有人呼叫了。
    //
    // 舊錨點在 `messageTree.js` 的 `persistAssistantTurn`。reserve 改寫之後送出
    // 路徑改走 `finalizeStreamedAssistant`(PUT 寫回預留列),全樹 grep
    // `persistAssistantTurn` 只剩定義與它自己的單元測試 —— 產品端零呼叫。
    // 錨點照樣命中一次、測試照樣紅(messageTree.test.js 直接呼叫它),所以
    // `--check-anchors` 和「既有測試抓到」都看不出異狀;唯一的徵兆是它在
    // **新測試**那一組存活 —— 掛起來的 shell 根本走不到那一行。
    //
    // 這正是這份清單存在的意義:一個「守著沒有人跑的碼」的守衛,是零。所以
    // 把同一個形狀(2xx-without-id 當成功)移到產品真的會跑的那一行。
    // ⚠ `persistAssistantTurn` 現在是死碼,該不該刪是產品決定,不在本輪範圍。
    find:
      '    if (saved && typeof saved.id === "number") {\n' +
      "      return { ok: true, saved, error: null, notice: null };",
    replace:
      '    if (saved || typeof saved.id === "number") {\n' +
      "      return { ok: true, saved, error: null, notice: null };",
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
    id: "agent-refresh-failure-feedback-hidden",
    file: "src/app.jsx",
    shape: "訊息被吃掉",
    intent: "agent reload 失敗不再顯示 inline feedback（選單空白但沒人說為什麼）",
    find:
      "        setAgentRefreshFeedback({\n" +
      "          kind: \"error\",\n" +
      "          message: `agent 載入失敗：${error.message || \"未知錯誤\"}`,\n" +
      "        });",
    replace: "        setAgentRefreshFeedback(null);",
  },
  {
    id: "agent-refresh-failure-claims-success",
    file: "src/app.jsx",
    shape: "失敗分支宣稱成功",
    intent: "agent reload 失敗時仍顯示已更新（使用者被誤導）",
    find:
      "        setAgentRefreshFeedback({\n" +
      "          kind: \"error\",\n" +
      "          message: `agent 載入失敗：${error.message || \"未知錯誤\"}`,\n" +
      "        });",
    replace:
      "        setAgentRefreshFeedback({\n" +
      "          kind: \"success\",\n" +
      "          message: \"agent 已更新\",\n" +
      "        });",
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
    // 真正會壞的是 abort 本身沒被呼叫。
    //
    // 2026-08-05 重新錨定。stopStreaming 從一行守衛長成一個區塊(按停止還要
    // 連帶取消排隊中、還沒開始串流的那幾輪)。這裡**刻意不**沿用「反轉
    // `if (controller)`」的舊做法:編輯重問與重新產生會在沒有任何串流時呼叫
    // stopStreaming(app.jsx 的 handleEditUser),反轉之後會對 undefined 呼叫
    // abort 而拋例外 —— 那是「當機」不是「按了沒反應」,兩種壞法混進同一個
    // 突變裡,測試紅了也證明不了停止鍵有守衛。
    //
    // 改成拿掉**呼叫運算子**:識別字一個沒少,abort 從此不發生,串流照樣跑到
    // 完 —— 這正是這個突變要編碼的那個使用者可見失敗。
    find:
      "      userStoppedRef.current.add(convId);\n" +
      "      controller.abort();",
    replace:
      "      userStoppedRef.current.add(convId);\n" +
      "      controller.abort;",
  },
  {
    id: "regenerate-not-branched",
    file: "src/app.jsx",
    shape: "條件反轉",
    intent: "重新產生不再開兄弟節點（舊答案被擠掉，分支歷史消失）",
    find: '          if (typeof assistantMsg.dbId === "number") {',
    replace: '          if (typeof assistantMsg.dbId === "string") {',
  },

  // ── transport:請求離開瀏覽器之前 ─────────────────────────────────
  //
  // 上面每一個突變都落在「請求送出之後」。這一區補的是**送出之前**那一格:
  // 標頭沒掛上去。它是本清單原本整個缺掉的象限,而缺掉的原因很典型 ——
  // 清單繼承了作者對「什麼會壞」的想像。實測過的後果:把 `X-CSRF-Token`
  // 從 api.js 或 sse.js 刪掉,兩套測試全綠,瀏覽器裡每一次送出都 403
  // (帶 cookie 不帶 header 打 POST /api/conversations → 403;帶了 → 201)。
  //
  // CSRF 的組裝在 shell 裡有**五份**,分別在三個檔;修好一份不會連帶
  // 修好其他四份,所以每一份都要有自己的突變。
  {
    id: "csrf-missing-on-control-plane",
    file: "src/runtime/api.js",
    shape: "標頭沒掛上去",
    intent: "控制面每一個 unsafe 請求都不帶 CSRF（送出/編輯/重試全部 403）",
    find:
      "    const csrf = readCsrfCookie();\n" +
      '    if (csrf && !out["X-CSRF-Token"] && !out["x-csrf-token"]) {',
    replace:
      "    const csrf = readCsrfCookie();\n" +
      '    if (!csrf && !out["X-CSRF-Token"] && !out["x-csrf-token"]) {',
  },
  {
    id: "csrf-missing-on-stream",
    file: "src/runtime/sse.js",
    shape: "標頭沒掛上去",
    intent: "串流請求不帶 CSRF（模型呼叫全部 403，聊天完全不能用）",
    // 同一行在 sse.js 出現兩次（streamChatCompletion 與 streamSessionAnswer），
    // 用後面那段註解鎖定前者。
    find:
      '    if (match) headers["X-CSRF-Token"] = decodeURIComponent(match[1]);\n' +
      "  }\n" +
      "  // Surface the conversation id to CSP so server-side latches",
    replace:
      '    if (!match) headers["X-CSRF-Token"] = decodeURIComponent(match[1]);\n' +
      "  }\n" +
      "  // Surface the conversation id to CSP so server-side latches",
  },
  {
    id: "csrf-missing-on-session-answer",
    file: "src/runtime/sse.js",
    shape: "標頭沒掛上去",
    intent: "續答（interrupt resume）不帶 CSRF，而且它自己抄了一份邏輯",
    find:
      '    if (match) headers["X-CSRF-Token"] = decodeURIComponent(match[1]);\n' +
      "  }\n" +
      "\n" +
      "  const url = `${(routerBaseUrl || \"\").replace(/\\/$/, \"\")}/v1/sessions/${encodeURIComponent(",
    replace:
      '    if (!match) headers["X-CSRF-Token"] = decodeURIComponent(match[1]);\n' +
      "  }\n" +
      "\n" +
      "  const url = `${(routerBaseUrl || \"\").replace(/\\/$/, \"\")}/v1/sessions/${encodeURIComponent(",
  },
  {
    id: "csrf-missing-on-task-create",
    file: "src/runtime/tasks.js",
    shape: "標頭沒掛上去（靜默降級）",
    intent: "建立 Task 403，而它的失敗契約是靜默回 null —— 用量從此掛不回任務",
    find:
      "    const csrf = readCsrfCookie();\n" +
      '    if (csrf) headers["X-CSRF-Token"] = csrf;',
    replace:
      "    const csrf = readCsrfCookie();\n" +
      '    if (!csrf) headers["X-CSRF-Token"] = csrf;',
  },
  {
    id: "conversation-id-header-not-attached",
    file: "src/runtime/sse.js",
    shape: "標頭沒掛上去（靜默 no-op）",
    intent: "這一輪不再告訴伺服器屬於哪個對話（機敏 latch、記憶寫入、附件注入全部靜悄悄地不作用）",
    find: '  if (typeof conversationId === "number") {',
    replace: '  if (typeof conversationId === "string") {',
  },
  {
    id: "task-id-header-not-attached",
    file: "src/runtime/sse.js",
    shape: "標頭沒掛上去（靜默 no-op）",
    intent: "這一輪不再掛回 Task（用量記錄與任務脫鉤，畫面上完全看不出來）",
    // 兩個條件同時反轉 = 恆偽,標頭從此不掛。只反轉第一個的話,taskId 不存在
    // 時反而會掛上一個字面上的 "undefined",那是另一種壞法,會把「沒掛上去」
    // 和「掛錯值」兩件事混在同一個突變裡。
    find: '  if (taskId !== undefined && taskId !== null && taskId !== "") {',
    replace: '  if (taskId === undefined && taskId === null && taskId !== "") {',
  },

  // ── 敏感資訊閘門與模式偏好 ────────────────────────────────────────
  //
  // 這一區守的東西和上面幾區不同:上面守「功能會不會壞」,這一區守
  // **「畫面上那句話會不會變成謊話」**。
  //
  // 兩個模式(warn/block)裡只有 block 真的攔得住東西離開瀏覽器,而且它
  // 現在是可以跨機器保存的使用者偏好。所以任何一條讓 block 靜默失效的改動,
  // 後果都不是「功能壞了」,是「使用者以為自己被保護著,而他不是」——
  // 這正是本專案第四條教訓(靜默成功比報錯危險)的形狀。
  //
  // ⚠ 每一個都實測過:改下去,`--check-anchors` 以外的舊套件不會紅。
  {
    id: "redaction-gate-bypassed",
    file: "src/chat.jsx",
    shape: "條件反轉",
    intent: "block 模式不再攔任何東西（使用者選了阻擋，身分證號照樣送出去）",
    // 2026-08-06 重新錨定:閘門改看 `blockingHits` 的結果(憑證進不了阻擋),
    // 原本那一行 `hits.length > 0` 不存在了。守的東西一字未變。
    find: '    if (mode === "block" && blocking.length > 0) {',
    replace: '    if (mode === "block" && blocking.length < 0) {',
  },
  {
    id: "redaction-gate-skipped-on-autosend",
    file: "src/chat.jsx",
    shape: "守衛被跳過（靜默 no-op）",
    intent: "預設提示詞的 autosend 繞過閘門（block 對範本送出完全無效）",
    // 這個破口真的存在過。閘門抽成共用函式之前,這條路自己呼叫 onSend。
    // 2026-08-05 重新錨定:被擋下來時現在會把範本內容放回輸入框(讓提示列與
    // 模式按鈕出現,使用者才走得出去),原本那一行單行 return 不存在了。
    // 改釘在條件本身 —— 不管擋下來之後要做什麼,這個判斷都得在。
    find: "                        if (!passesRedactionGate(bodyHits)) {",
    replace: "                        if (!passesRedactionGate(bodyHits) && false) {",
  },
  {
    id: "redaction-gate-skipped-before-title",
    file: "src/app.jsx",
    shape: "守衛被跳過（靜默 no-op）",
    intent: "送出路徑最前面那道閘門失效（訊息本身擋住了，對話標題與 Task 標題照樣帶著身分證號出去）",
    // 這一行守的東西**兩個扼流點都來不及守**:ensureConversation 與
    // createTaskForConversation 都在它們之前跑,而且都拿這段草稿當標題送上去。
    //
    // 釘子是 redactionChokePoint 的「Task 標題」那一條:它走建議追問
    // (`app.jsx` 把它接成 `onPickFollowUp={(q) => sendMessage(q, [], {})}`,
    // 直接進 sendMessage,不經過 composer 閘門),並讓 Task 建立一直失敗,
    // 所以每一輪都會再送一次標題。
    //
    // ⚠ 走 composer 的那條路**釘不住**這一行:chat.jsx 的閘門會先擋下來,
    // 於是拿掉這一行整套照樣全綠。這個突變存在的理由就是把那件事說出來。
    find: "    if (!passesRedactionGate(text)) return;",
    replace: "    if (!passesRedactionGate(text) && false) return;",
  },
  {
    id: "redaction-choke-point-stream-removed",
    file: "src/app.jsx",
    shape: "扼流點被拆掉（靜默 no-op）",
    intent: "模型呼叫的扼流點失效（編輯重問／引導式重試／建議提示／對比模式全部繞過阻擋）",
    // 這是「呼叫端清單」退化回來的那個突變。閘門要留在所有人都必須經過的
    // 地方,而不是留在每一個發起點 —— 後者已經漏掉兩次。
    find: "    if (!passesRedactionGate(outgoingUserText(opts?.payload))) {",
    replace: "    if (!passesRedactionGate(outgoingUserText(opts?.payload)) && false) {",
  },
  {
    id: "redaction-choke-point-persist-removed",
    file: "src/app.jsx",
    shape: "扼流點被拆掉（靜默 no-op）",
    intent: "落庫扼流點失效（模型沒看到，但資料庫裡照樣存了一份原文）",
    find: "    if (!passesRedactionGate(content)) {",
    replace: "    if (!passesRedactionGate(content) && false) {",
  },
  {
    id: "redaction-outgoing-text-blind",
    file: "src/app.jsx",
    shape: "存取器回空值",
    intent: "扼流點永遠看到空字串（閘門還在、但再也偵測不到任何東西）",
    // 最安靜的那一種:兩個扼流點都還在,`--check-anchors` 全綠,
    // 而它們檢查的永遠是空字串。
    find: "  const msgs = Array.isArray(payload?.messages) ? payload.messages : [];",
    replace: "  const msgs = Array.isArray(payload?.messages) && [];",
  },
  {
    id: "redaction-mode-not-persisted",
    file: "src/app.jsx",
    shape: "存取器回空值",
    intent: "使用者選的模式不再存回後端（重新整理就被放寬回預設，畫面卻說會保留）",
    find: "      putUiSettings(authRequest, { folders, redactionMode }).catch(() => { /* best-effort */ });",
    replace: "      putUiSettings(authRequest, { folders }).catch(() => { /* best-effort */ });",
  },
  {
    id: "redaction-mode-whitelist-dropped",
    file: "src/app.jsx",
    shape: "驗證放寬",
    intent: "讀回來的模式不再白名單驗證（不明值讓提示列說「將阻擋送出」而實際照送）",
    find: "        if (alive && REDACTION_MODES.includes(s.redactionMode)) {",
    replace: "        if (alive && typeof s.redactionMode === \"string\") {",
  },

  // ── 偵測器本身:認得的形狀與認錯的機率 ──────────────────────────────
  //
  // 上面那一區守「閘門會不會被繞過」。這一區守**閘門看到的東西對不對** ——
  // 一個誤報成災的偵測器不會壞掉,它只是會被使用者學會忽略,然後在真的有
  // 東西的那一天靜靜地失效。所以這裡每一個突變都對應一個**使用者可見的
  // 誤報或漏報**,而不是一行程式碼。
  {
    id: "id-check-digit-not-verified",
    file: "src/data.jsx",
    shape: "驗證放寬",
    intent: "身分證只比形狀不驗檢查碼（採購案號、料號、財產編號全部變成「疑似身分證」）",
    find: "    validate: isTaiwanIdNumber,",
    replace: "    validate: isTaiwanIdNumber && undefined,",
  },
  {
    id: "id-second-digit-unconstrained",
    file: "src/data.jsx",
    shape: "條件放寬",
    intent: "身分證第二碼不再限性別碼／新式證號碼（多一批院內編號進來排隊碰運氣）",
    find: "    regex: /\\b[A-Z][1289]\\d{8}\\b/g,",
    replace: "    regex: /\\b[A-Z][0-9]\\d{8}\\b/g,",
  },
  {
    id: "card-luhn-not-verified",
    file: "src/data.jsx",
    shape: "驗證放寬",
    intent: "信用卡不驗 Luhn（年度欄「2021 2022 2023 2024」變成一張卡）",
    find: "    validate: passesLuhn,",
    replace: "    validate: passesLuhn && undefined,",
  },
  {
    id: "card-separator-swallows-newline",
    file: "src/data.jsx",
    shape: "字元類別放寬",
    intent: "信用卡分隔符又含換行與 tab（任何從表格貼上的四欄四位數都是一張卡）",
    // `[- ]` → `[-\s]` 就是改動前的原樣。這一個突變等於把整包最主要的
    // 誤報來源放回去。
    find: "    regex: /\\b[2-6]\\d{3}([- ]?)\\d{4}\\1\\d{4}\\1\\d{4}\\b/g,",
    replace: "    regex: /\\b[2-6]\\d{3}([-\\s]?)\\d{4}\\1\\d{4}\\1\\d{4}\\b/g,",
  },
  {
    id: "card-first-digit-unconstrained",
    file: "src/data.jsx",
    shape: "條件放寬",
    intent: "信用卡首碼不再限發卡產業別（1 開頭的十六位序號只要湊巧過 Luhn 就命中）",
    find: "    regex: /\\b[2-6]\\d{3}([- ]?)\\d{4}\\1\\d{4}\\1\\d{4}\\b/g,\n    validate: passesLuhn,",
    replace: "    regex: /\\b\\d{4}([- ]?)\\d{4}\\1\\d{4}\\1\\d{4}\\b/g,\n    validate: passesLuhn,",
  },
  {
    id: "email-tld-accepts-digits",
    file: "src/data.jsx",
    shape: "字元類別放寬",
    intent: "Email 的頂級網域又可以是數字（貼一段程式碼，`react-dom@18.3.1` 就是一個人的信箱）",
    find: "    regex: /[\\w.+-]+@[\\w-]+(?:\\.[\\w-]+)*\\.[A-Za-z]{2,}\\b/g,",
    replace: "    regex: /[\\w.+-]+@[\\w-]+(?:\\.[\\w-]+)*\\.[\\w.-]+/g,",
  },
  {
    id: "api-key-not-detected",
    file: "src/data.jsx",
    shape: "偵測靜默失效",
    intent: "API 金鑰完全偵測不到（貼一把 sk- 金鑰進去，一個字都不會說）",
    // `\b` → `\B`:識別字一個沒少,而每一把貼在空白或行首後面的金鑰都不再命中。
    find: "    regex: /\\b(?:sk-(?:proj|ant-api",
    replace: "    regex: /\\B(?:sk-(?:proj|ant-api",
  },
  {
    id: "bearer-token-not-detected",
    file: "src/data.jsx",
    shape: "偵測靜默失效",
    intent: "`Authorization: Bearer …` 偵測不到（JWT 那一半還在，所以測試不紅就代表沒人守 Bearer）",
    find: "    regex: /\\bBearer[ \\t]+",
    replace: "    regex: /\\BBearer[ \\t]+",
  },
  {
    id: "private-key-block-not-detected",
    file: "src/data.jsx",
    shape: "偵測靜默失效",
    intent: "貼一整段私鑰進來完全沒有提醒（最不該漏的那一種）",
    find: "PRIVATE KEY(?: BLOCK)?-----/g,",
    replace: "PRIVATE  KEY(?: BLOCK)?-----/g,",
  },
  {
    id: "chinese-password-not-detected",
    file: "src/data.jsx",
    shape: "偵測靜默失效",
    intent: "中文的「密碼：…」偵測不到（英文的還在，所以這裡不紅＝中文使用者沒人守）",
    find: "    regex: /(?:[Pp]assword|PASSWORD|[Pp]asswd|[Pp]wd|密碼)[ \\t]*",
    replace: "    regex: /(?:[Pp]assword|PASSWORD|[Pp]asswd|[Pp]wd)[ \\t]*",
  },
  {
    id: "sk-prefix-length-unpinned",
    file: "src/data.jsx",
    shape: "條件放寬（回到驗收抓到的那一版）",
    intent: "`sk-` 又變成「三字元前綴＋16 字」（貼一段前端程式碼，SpinKit 的 CSS class 就是一把 API 金鑰）",
    find: "sk-(?:proj|ant-api\\d{2})-[A-Za-z0-9_-]{40,}|sk-[A-Za-z0-9]{32,}",
    replace: "sk-[A-Za-z0-9_-]{16,}",
  },
  {
    id: "password-value-accepts-code-shapes",
    file: "src/data.jsx",
    shape: "字元類別放寬（回到驗收抓到的那一版）",
    intent: "`password:` 後面又什麼都收（`varchar(255)`、`z.string().min(8)`、`${DB_PASSWORD}` 全部變成疑似密碼）",
    find: '(?=[A-Za-z0-9._~+\\/=!@#%^&*-]*[0-9])[A-Za-z0-9._~+\\/=!@#%^&*-]{8,}/g,',
    replace: '(?=[^\\s"\'<>]*[0-9!@#$%^&*+=?~])[^\\s"\'<>]{6,}/g,',
  },
  {
    id: "blocking-whitelist-not-frozen",
    file: "src/data.jsx",
    shape: "保證被拿掉（而且沒有人會發現）",
    intent: "阻擋白名單變成可以就地改的陣列（任何一段程式碼都能把 credential 加成擋人的一族）",
    find: 'export const BLOCKING_FAMILIES = Object.freeze(["pii"]);',
    replace: 'export const BLOCKING_FAMILIES = ["pii"];',
  },
  {
    id: "credential-becomes-blocking",
    file: "src/data.jsx",
    shape: "白名單被放寬",
    intent: "憑證變成擋得住送出（貼一段含金鑰的程式碼就送不出去 —— 這正是本包要避免的那件事）",
    find: 'export const BLOCKING_FAMILIES = Object.freeze(["pii"]);',
    replace: 'export const BLOCKING_FAMILIES = Object.freeze(["pii", "credential"]);',
  },
  {
    id: "composer-gate-ignores-family",
    file: "src/chat.jsx",
    shape: "分流被繞過",
    intent: "輸入框閘門不再分辨個資與憑證（block 模式下貼金鑰就送不出去）",
    // ⚠ 突變本身要挑「看起來很無害」的那一種:個資有命中時行為完全一樣,
    // 只有在**只剩憑證**的時候才退回去擋人。這正是未來會被誰隨手加上去的
    // 那一行 fallback。
    find: "    const blocking = blockingHits(hits);",
    replace: "    const blocking = blockingHits(hits).length ? blockingHits(hits) : hits;",
  },
  {
    id: "choke-point-gate-ignores-family",
    file: "src/app.jsx",
    shape: "分流被繞過",
    intent: "扼流點不再分辨個資與憑證（範本、重試、對比模式裡貼金鑰全部送不出去）",
    find: '    const blocking = blockingHits(detectPII(text || ""));',
    replace:
      '    const blocking = blockingHits(detectPII(text || "")).length' +
      ' ? blockingHits(detectPII(text || "")) : detectPII(text || "");',
  },
  {
    id: "hint-bar-claims-block-for-credentials",
    file: "src/trust.jsx",
    shape: "畫面上那句話變成謊話",
    intent: "草稿裡只有金鑰時提示列說「這則不會送出」，而它送得出去",
    find: '  const willBlock = mode === "block" && blockingHits(hits).length > 0;',
    replace: '  const willBlock = mode === "block" && hits.length > 0;',
  },

  {
    id: "redaction-mode-lost-in-compare",
    file: "src/multiagent.jsx",
    shape: "props 沒傳下去（靜默 no-op）",
    intent: "對比模式的輸入框收不到模式，退回預設 warn（block 在對比模式裡靜默失效）",
    find: "          redactionMode={redactionMode}",
    replace: "          redactionMode={undefined}",
  },
];

// ---- 執行 ------------------------------------------------------------------

// Every test invocation is isolated from the checker: a hung mutation must not
// make the checker hang forever, and a runaway V8 heap must not take the host
// down with it. The timeout is deliberately per child, not per mutation, since
// one mutation runs several independent test groups.
const CHILD_TIMEOUT_MS = 20_000;
const CHILD_KILL_GRACE_MS = 1_000;
const CHILD_MAX_OLD_SPACE_MB = 512;

/** @type {import("node:child_process").ChildProcess | null} */
let activeChild = null;
/** @type {"SIGINT" | "SIGTERM" | "SIGHUP" | null} */
let parentStopSignal = null;

function childEnvironment() {
  const inheritedNodeOptions = process.env.NODE_OPTIONS?.trim();
  return {
    ...process.env,
    CI: "1",
    NODE_OPTIONS: [
      inheritedNodeOptions,
      `--max-old-space-size=${CHILD_MAX_OLD_SPACE_MB}`,
    ]
      .filter(Boolean)
      .join(" "),
  };
}

/** Terminate the complete detached child process group, with a direct fallback. */
function terminateChild(child, signal) {
  if (!child?.pid) return;
  try {
    // `detached: true` gives the test child its own process group, so workers
    // spawned by npx/vitest cannot survive a timeout or a genuine interrupt.
    process.kill(-child.pid, signal);
    return;
  } catch {
    // A platform without negative-PID process-group support, or a group that
    // exited between the check and kill, still gets a best-effort direct kill.
  }
  try {
    process.kill(child.pid, signal);
  } catch (err) {
    if (err.code !== "ESRCH") {
      console.error(`無法以 ${signal} 結束測試子行程 ${child.pid}: ${err.message}`);
    }
  }
}

/**
 * Run one Vitest child without confusing its wait status with a signal sent to
 * this checker. A child-only signal is returned as a hard-stop result; only a
 * SIGINT observed by this parent is a genuine user interrupt.
 */
function runVitest(args) {
  if (parentStopSignal) {
    return Promise.resolve({
      green: false,
      out: "",
      kind: "parent-signal",
      signal: parentStopSignal,
    });
  }

  return new Promise((resolve) => {
    let stdout = "";
    let stderr = "";
    let settled = false;
    let timedOut = false;
    let timeoutTimer;
    let killTimer;
    const child = spawn("npx", ["vitest", "run", ...args], {
      cwd: ROOT,
      detached: true,
      stdio: ["ignore", "pipe", "pipe"],
      env: childEnvironment(),
    });

    activeChild = child;

    const finish = (result) => {
      if (settled) return;
      settled = true;
      clearTimeout(timeoutTimer);
      clearTimeout(killTimer);
      if (activeChild === child) activeChild = null;
      resolve(result);
    };

    child.stdout.on("data", (chunk) => {
      stdout += chunk.toString();
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk.toString();
    });

    timeoutTimer = setTimeout(() => {
      timedOut = true;
      terminateChild(child, "SIGTERM");
      killTimer = setTimeout(() => terminateChild(child, "SIGKILL"), CHILD_KILL_GRACE_MS);
    }, CHILD_TIMEOUT_MS);

    child.once("error", (err) => {
      finish({
        green: false,
        out: `${stdout}${stderr}${err.message}\n`,
        kind: "spawn-error",
        error: err,
      });
    });
    child.once("close", (code, signal) => {
      const out = `${stdout}${stderr}`;
      if (parentStopSignal) {
        finish({
          green: false,
          out,
          kind: "parent-signal",
          signal: parentStopSignal,
        });
      } else if (timedOut) {
        finish({
          green: false,
          out,
          kind: "timeout",
          signal,
          timeoutMs: CHILD_TIMEOUT_MS,
        });
      } else if (signal) {
        finish({ green: false, out, kind: "child-signal", signal });
      } else {
        finish({
          green: code === 0,
          out,
          kind: code === 0 ? "success" : "test-failure",
          exitCode: code,
        });
      }
    });
  });
}

const runNew = () => runVitest(NEW_TESTS);
const runPreExisting = () =>
  runVitest(PRE_EXISTING_EXCLUDES.flatMap((e) => ["--exclude", e]));

class GenuineInterrupt extends Error {
  constructor() {
    super("the checker received SIGINT");
    this.name = "GenuineInterrupt";
  }
}

class UnexpectedChildStop extends Error {
  constructor(result) {
    super(
      result.kind === "child-signal"
        ? `the test child was terminated by ${result.signal}`
        : result.kind === "parent-signal"
          ? `the parent received ${result.signal}`
          : `the test child could not start: ${result.error?.message || "unknown spawn error"}`,
    );
    this.name = "UnexpectedChildStop";
    this.result = result;
  }
}

function ensureChildDidNotStop(result) {
  if (result.kind === "parent-signal" && result.signal === "SIGINT") {
    throw new GenuineInterrupt();
  }
  if (
    result.kind === "parent-signal" ||
    result.kind === "child-signal" ||
    result.kind === "spawn-error"
  ) {
    throw new UnexpectedChildStop(result);
  }
}

function summarise(out) {
  const plain = out.replace(/\u001b\[[0-?]*[ -/]*[@-~]/g, "");
  const m = plain.match(/(?:^|\n)\s*Tests\s+(.+)/);
  return m ? m[1].trim() : "（無法解析）";
}

function testResultLabel(result) {
  if (result.green) return "綠 ✗（存活）";
  if (result.kind === "timeout") return `逾時 ✓（${result.timeoutMs}ms 上限）`;
  return "紅 ✓";
}

// ---- 中途中止的還原 --------------------------------------------------------
//
// 這個腳本會把 production 檔案改壞再改回來。中途被打斷而沒有還原,工作目錄
// 就留著一個改壞的檔案**而且不吭聲** —— 下一個人會以為那是別人寫的碼。
// 2e082489 的版本就是這樣:Ctrl-C 之後 app.jsx 停在
// `messagesByConv[convId] && []`,git status 只說「M app.jsx」。
//
// 子行程現在用非同步方式執行,所以本行程能在測試跑著時收到訊號。Ctrl-C
// 的判斷只看**本行程實際收到的 SIGINT**;子行程的 wait status 不會被當成
// 使用者意圖。測試子行程另放進自己的 process group,讓 timeout 和 Ctrl-C
// 都能連同 Vitest workers 一起收乾淨。

/** @type {Map<string, string>} 路徑 → 原始內容 */
const pendingRestores = new Map();

// 放 node_modules/.cache 下:那裡一定在 .gitignore 裡,不會有人不小心 commit
// 一份 production 原始碼的副本進 PUBLIC repo。
const JOURNAL = resolve(ROOT, "node_modules/.cache/anila-mutation-check.json");

function writeJournal() {
  mkdirSync(dirname(JOURNAL), { recursive: true });
  writeFileSync(
    JOURNAL,
    JSON.stringify({ at: new Date().toISOString(), files: [...pendingRestores] }, null, 2),
    "utf8",
  );
}

function clearJournal() {
  if (existsSync(JOURNAL)) rmSync(JOURNAL, { force: true });
}

function restoreAll() {
  for (const [path, original] of pendingRestores) {
    writeFileSync(path, original, "utf8");
  }
  pendingRestores.clear();
  clearJournal();
}

/** 啟動時把上一輪沒還原完的東西修回去。回傳修了幾個檔。 */
function recoverFromJournal() {
  if (!existsSync(JOURNAL)) return 0;
  let entry;
  try {
    entry = JSON.parse(readFileSync(JOURNAL, "utf8"));
  } catch {
    console.error(`還原日誌 ${JOURNAL} 讀不動 —— 請自己確認工作目錄狀態。`);
    return -1;
  }
  const files = entry.files || [];
  for (const [path, original] of files) {
    if (readFileSync(path, "utf8") !== original) writeFileSync(path, original, "utf8");
  }
  clearJournal();
  if (files.length > 0) {
    console.error(
      `⚠ 上一輪(${entry.at})中途中止,已把 ${files.length} 個檔案還原:\n` +
        files.map(([p]) => `    ${p}`).join("\n"),
    );
  }
  return files.length;
}

for (const signal of ["SIGINT", "SIGTERM", "SIGHUP"]) {
  process.on(signal, () => {
    if (parentStopSignal) return;
    parentStopSignal = signal;
    if (activeChild) {
      terminateChild(activeChild, signal === "SIGINT" ? "SIGINT" : "SIGTERM");
    }
  });
}
// 未捕捉的例外同樣不能把改壞的檔案留在原地。
process.on("uncaughtException", (err) => {
  restoreAll();
  console.error(err);
  reportIncomplete("an unexpected checker error");
  process.exit(2);
});

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
  const mutated = original.replace(mut.find, mut.replace);
  if (mutated === original) {
    throw new Error(
      `突變 ${mut.id}: find 與 replace 產生完全相同的內容 —— 這不是突變。`,
    );
  }
  // 先落日誌再改檔 —— 順序反過來的話,兩者之間被 kill -9 就沒人知道要修什麼。
  pendingRestores.set(path, original);
  writeJournal();
  writeFileSync(path, mutated, "utf8");
  return () => {
    writeFileSync(path, original, "utf8");
    pendingRestores.delete(path);
    if (pendingRestores.size === 0) clearJournal();
    else writeJournal();
  };
}

const mutationProgress = { total: 0, completed: 0 };

function mutationsNotRun() {
  return Math.max(mutationProgress.total - mutationProgress.completed, 0);
}

function reportIncomplete(reason) {
  console.error(
    `\nMutation check stopped: ${reason}. ${mutationsNotRun()} mutations did not run.`,
  );
}

async function main() {
  const argv = process.argv.slice(2);
  // 任何模式(含 --list / --check-anchors)都先修上一輪的殘留 —— 沒有比
  // 「在一個被上一輪改壞的樹上驗錨點」更會誤導人的事。
  if (recoverFromJournal() < 0) return 2;
  if (argv.includes("--restore")) {
    console.log("還原日誌已處理完畢。");
    return 0;
  }
  if (argv.includes("--list")) {
    for (const m of MUTATIONS) console.log(`${m.id}\t${m.file}\t${m.intent}`);
    return 0;
  }
  if (argv.includes("--check-anchors")) {
    // 不跑測試,只驗每個錨點在目標檔剛好命中一次、而且 replace 真的不同。
    // 原始碼一動就會有錨點漂掉,這個模式讓那件事在幾毫秒內被說出來,
    // 而不是在跑到第 17 個突變時才炸。
    let bad = 0;
    for (const mut of MUTATIONS) {
      const body = readFileSync(resolve(ROOT, mut.file), "utf8");
      const hits = body.split(mut.find).length - 1;
      const noop = body.replace(mut.find, mut.replace) === body;
      const ok = hits === 1 && !noop;
      if (!ok) bad += 1;
      console.log(
        `${ok ? "ok  " : "BAD "} ${mut.id}\t命中 ${hits} 處${noop ? "、且 replace 與原文相同" : ""}`,
      );
    }
    console.log(`\n${MUTATIONS.length - bad} / ${MUTATIONS.length} 個錨點唯一且非空操作。`);
    return bad === 0 ? 0 : 2;
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
  mutationProgress.total = selected.length;
  mutationProgress.completed = 0;

  console.log("== 前置:確認未突變時兩組都是綠的 ==");
  const baseNew = await runNew();
  ensureChildDidNotStop(baseNew);
  if (!baseNew.green) {
    console.error("新測試在乾淨狀態下就是紅的，先修好再跑突變檢查。");
    console.error(baseNew.out.slice(-3000));
    reportIncomplete("the clean new-test baseline failed");
    return 2;
  }
  console.log(`  新行為測試: ${summarise(baseNew.out)}`);
  const basePre = await runPreExisting();
  ensureChildDidNotStop(basePre);
  // 這一關就是整份報告裡「既有測試只抓到 N 個」那個數字的全部價值所在。
  // 少了它，一條本來就紅的既有測試會被算成「被這個突變殺掉」，而兩者在
  // 輸出上長得一模一樣。基準線不綠 = 這一輪量不出東西，直接離開。
  if (!basePre.green) {
    console.error(
      "既有測試在乾淨狀態下就是紅的 —— 這一輪量不出「既有測試抓到幾個」。\n" +
        "  （紅著的既有測試在每一個突變下都會紅，會被誤記成突變被抓到。）\n" +
        "  先讓既有測試回到綠，再跑突變檢查。",
    );
    console.error(basePre.out.slice(-3000));
    reportIncomplete("the clean pre-existing-test baseline failed");
    return 2;
  }
  console.log(`  既有測試:   ${summarise(basePre.out)}`);
  console.log("");

  const results = [];
  for (const mut of selected) {
    process.stdout.write(`-- ${mut.id} … `);
    let restore;
    try {
      restore = applyMutation(mut);
      const rNew = await runNew();
      ensureChildDidNotStop(rNew);
      const rPre = await runPreExisting();
      ensureChildDidNotStop(rPre);
      results.push({
        id: mut.id,
        file: mut.file,
        shape: mut.shape,
        intent: mut.intent,
        caughtByNew: !rNew.green,
        caughtByPreExisting: !rPre.green,
      });
      console.log(
        `新測試 ${testResultLabel(rNew)} / 既有測試 ${
          rPre.green ? "綠" : rPre.kind === "timeout" ? `逾時（${rPre.timeoutMs}ms 上限）` : "紅"
        }`,
      );
    } finally {
      if (restore) restore();
    }
    // 還原之後兩組都要回到綠。這既是「工作目錄沒被弄髒」的檢查，也是
    // **下一個突變的前置綠燈** —— 每一個突變都從一個已知全綠的狀態出發，
    // 而不是沿用一開始那次基準線的結論。
    const afterNew = await runNew();
    ensureChildDidNotStop(afterNew);
    if (!afterNew.green) {
      console.error(`還原後新測試在 ${mut.id} 仍是紅的 — 工作目錄可能已污染，中止。`);
      console.error(afterNew.out.slice(-3000));
      reportIncomplete(`the restored new-test check failed after ${mut.id}`);
      return 2;
    }
    const afterPre = await runPreExisting();
    ensureChildDidNotStop(afterPre);
    if (!afterPre.green) {
      console.error(
        `還原後既有測試在 ${mut.id} 仍是紅的 —— 後面每一個突變的「既有測試抓到」` +
          `都會變成假的，中止。`,
      );
      console.error(afterPre.out.slice(-3000));
      reportIncomplete(`the restored pre-existing-test check failed after ${mut.id}`);
      return 2;
    }
    mutationProgress.completed += 1;
  }

  if (results.length !== selected.length || mutationProgress.completed !== selected.length) {
    reportIncomplete("the mutation loop did not visit every selected mutation");
    return 2;
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

main()
  .then((code) => process.exit(code))
  .catch((err) => {
    restoreAll();
    if (err instanceof GenuineInterrupt) {
      reportIncomplete("genuine SIGINT interrupt");
      process.exit(130);
    }
    if (err instanceof UnexpectedChildStop) {
      reportIncomplete(err.message);
      process.exit(1);
    }
    console.error(err);
    reportIncomplete("an unexpected checker error");
    process.exit(2);
  });
