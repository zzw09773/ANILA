# Shell 評分回饋位置與 CSP 被評分回覆檢視

基準：main @ bfb6ee6567d7c8de7754df482c68654fcb636ee5；修改保留工作樹，未 commit、push、部署。2026-09-18 開始，2026-09-19 完成前端範圍驗證。

## 修改

- Shell：已送出回饋的操作列／分數列，在非 hover、非 focus-within 時收合高度；觸控保留操作列。感謝回饋不再隔著兩列透明空白。一般未送出訊息不改收合行為。
- Shell：handleRate 明確回傳儲存成功／失敗；回饋送出等待結果，失敗保留評語供重試，等待期間停用送出按鈕。
- CSP：每筆回饋提供「查看被評分回覆」。點擊才呼叫既有 GET /api/conversations/{id}?view=all；精確比對 message_id 與 assistant role，沿 parent_id 找最近使用者提問。無相鄰列／active branch 猜測。
- CSP：正文採文字插值；載入、錯誤、找不到訊息與重試可見；關閉／切列／卸載清除內容並使舊請求失效。不更改後端、權限、稽核、回饋列表或 CSV 契約。
- 保留上一輪視覺修改、使用者的 settings sync 與 CSP threshold 修正。

## 驗證與界線

- Shell npm test：90 檔、951 測試通過。新增實際 App + fake backend 回歸驗證 PUT 失敗後保留評語、重試成功才顯示感謝。
- CSP npm test：35 個測試檔項目通過。新增真實 SFC client build + DOM mount 測試，包含非活躍分支、missing、403、retry、close/switch race；非僅原始碼字串比對。輔助函式測試含 parent 缺失與循環。
- 兩個 npm run build 通過；仍有既有大 chunk 警告。git diff --check 通過。
- CUA 真瀏覽器搭配合成資料：Shell hover 時控制列高度 30 與 25.2px、感謝提示 y=279.4；滑鼠移至空白處高度都為 0、提示 y=208.2，向上收合 71.2px。Tab 切入後控制列重新展開。
- CSP 瀏覽器：顯示訊息 #12 與其 parent #10 提問；未顯示同分支其他回答。缺失目標 #99 明確提示找不到。
- 臨時頁面與兩個 loopback Vite 服務已關閉。
- 尚未執行正式後端 browser-to-storage E2E，未部署。L2 包；目前可見 task／工具沒有可呼叫的 GLM 與 Qwen 路由，因此獨立 GLM review／Qwen QA 未完成，不能作為 release acceptance。

## 派工與證據

- Cursor Grok CLI session：1b1804c6-8c6c-4845-aa1b-3dc7467680ed。工作者完成指定 UI 產物，工具受限而未能跑測試；父層讀回 diff、修正測試 harness 與介面串接並完成上述檢查。
- JEV jev-1.13.0：需求需精確回覆而非只有 ID，0.95。最終方案符合需求判斷 0.75（未達 0.8，採直接 DOM／行為測試核實）；完整 production E2E 判斷 0.03（低於 0.2，不支持）。JEV 不代替執行證據。
- 臨時 logs、packet、worker 回報與截圖：/tmp/anila-feedback-fix-20260918/。截圖 shell-feedback-collapsed.png、csp-rated-reply.png；均為合成資料。
- 長期維護項目：CSP 的訊息樹定位 helper／請求序號與相應測試，依賴既有 message id、role、parent_id、view=all 契約；Shell CSS 收合規則需維持 hover、鍵盤與 touch 三種操作方式。
