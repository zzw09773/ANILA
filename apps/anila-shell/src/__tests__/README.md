# anila-shell 測試:強度分級與突變檢查

## 為什麼有這份文件

2026-08-03 的稽核測到一件事:把 `app.jsx` 的對話歷史組裝弄壞(讓每一回合
都送出空歷史),**全套 413 條測試一條都沒紅**。產品當時是壞的——使用者每
一句話都在沒有前文的情況下送給模型——而測試是綠的。

原因不是測試寫得少(33 個檔、16 個做元件渲染),而是**沒有任何一條把
orchestrator 掛起來**。`app.jsx` 是對話狀態機、歷史組裝、存檔與錯誤呈現
的所在地,而它唯一被 import 的測試只取了一個純函式;其餘關於它的斷言全
部是「讀原始碼字串比對」。

## 三級強度

| 級別 | 長相 | 擋得住什麼 | 擋不住什麼 |
|---|---|---|---|
| **行為測試** | 掛起真的元件/orchestrator,操作它,斷言使用者看得到的結果 | 邏輯錯、時序錯、狀態沒接上、靜默失敗 | — |
| **純函式單元測試** | 直接呼叫 `runtime/*.js` 的匯出函式 | 該函式自己的邏輯 | 呼叫端根本沒接、參數傳錯 |
| **原始碼字串比對** | `readFileSync` + `toContain` | 整段被刪掉 | 幾乎所有其他情況 |

第三級**不是行為覆蓋率**。它們有價值(擋整段刪除、擋文案倒退),但不能
拿來回答「這個功能會動嗎」。所有這類檔案都在檔頭標了 `@source-text-guard`,
`sourceTextGuardRegistry.test.js` 會確保新增的也一定要標。

## orchestrator 行為測試

| 檔案 | 釘住的不變式 |
|---|---|
| `orchestratorSend.test.jsx` | 送出時帶的對話歷史正確(含第三輪之後);回答真的存進後端 |
| `orchestratorFailures.test.jsx` | 存檔失敗/2xx-without-id/後端非 2xx **一定被使用者看見** |
| `orchestratorBranching.test.jsx` | 編輯重問與重新產生的歷史切點、`/branch` 兄弟節點 |
| `orchestratorConversations.test.jsx` | 切換對話不互相污染;接續既有對話帶得到伺服器上的前文 |

### 掛載方式

`helpers/orchestrator.jsx` 的 `mountOrchestrator()` render 的是 **`src/app.jsx`
的 default export**,包在 `main.jsx` 用的同一組 Provider(`AuthProvider` +
`ConfirmProvider`)裡。連登入態都是真的:`AuthProvider` 自己去打
`/api/auth/me`,由假後端回答。

`helpers/fakeBackend.js` 攔的是全域 `fetch`。app.jsx 所有對外路徑
(`authRequest` / `streamChatCompletion` / `createTaskForConversation` /
`refreshAgents`)最後都收斂到那裡,所以攔在這一層,受測的是**真的
orchestrator**,沒有任何一個它自己的函式被替身取代。

> ⚠ 不要在 harness 裡「重新實作一遍」app.jsx 的任何邏輯。一旦這麼做,
> 測試就變成在測 harness 自己——那正是上一輪全綠但產品壞掉的成因。

假後端是**有狀態**的:訊息樹(`parent_id` / `sibling_*`)、active leaf、
對話列表都真的維護,所以「多輪之後歷史還是對的」驗得出來。

## 突變檢查(證明測試真的會紅)

一條「把它宣稱保護的行為還原回去、卻仍然通過」的測試等於不存在。
`scripts/mutation-check.mjs` 就是用來證明不是這樣的。

```bash
cd apps/anila-shell
node scripts/mutation-check.mjs           # 全部突變
node scripts/mutation-check.mjs history-* # 只跑符合的
node scripts/mutation-check.mjs --list    # 看清單
```

每個突變會:確認乾淨時是綠的 → 套用 → 跑新測試與既有測試兩組 →
還原 → 再確認回到綠。任何突變**存活**(該紅卻沒紅)離開碼為 1。

設計約束:每個突變都**保留所有識別字**,改的是運算子、索引、屬性名這種
東西——所以 grep 原始碼的測試救不了你。清單裡至少有兩個是「讓存取器回
空值」與「刪掉一個賦值」那一型,也就是當初讓 413 條全綠的那一型。

2026-08-03 的結果:**新測試 16/16 抓到,既有測試 2/16**。

過程中有兩個候選突變被判定為**等價突變**(改了但行為不可能不同),
已在清單裡改成真的會壞的版本並註明原因 —— 等價突變存活不代表測試有洞,
把它當成洞去補會補出假測試。

錨點必須在目標檔案裡剛好命中一次,否則腳本會直接報錯離開——原始碼改動
後突變清單需要跟著更新,這是刻意讓它吵的。
