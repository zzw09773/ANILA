# Shell / CSP 簡約介面優化驗證

- 日期：2026-09-18；工作樹：main；基準 SHA：bfb6ee6567d7c8de7754df482c68654fcb636ee5。
- 狀態：修改留在工作樹，未 commit、push 或部署。此紀錄是前端範圍驗證，不是 release acceptance。
- 方向：依使用者要求採簡約、內容優先設計。Shell 使用靜態小型識別、低調快捷按鈕、中性色與深淺色文字對比；CSP 使用中性頁首、系統字型標題與較少表單裝飾。
- 可用性：Shell Modal 限制視窗高度、內容區捲動、保留 Escape 與焦點回復；CSP 設定表在窄版轉堆疊欄位，補上錯誤欄位關聯、狀態提示、具上下文的儲存按鈕名稱。
- 保留使用者既有修改：folderSettingsSync.test.jsx、platform_setting.py、test_kb_threshold_setting.py，以及 app.jsx 的設定同步與失敗重試修正。本輪 app.jsx 僅調整主題預設與 EmptyState 呈現。

## 驗證

- Shell：88 個測試檔、942 個測試通過；production build 通過。
- CSP：node 測試執行器 33 個測試檔項目通過；production build 通過。
- 原有設定讀取失敗、儲存失敗重試情境：使用實際 App 與假後端的額外回歸測試 2/2 通過。
- git diff --check 通過。兩個建置仍有 chunk size 警告。
- 瀏覽器使用實際來源元件與合成測試資料：CSP 桌面 1280×800，以及窄版 390×640 深淺色檢查；窄版主區 clientWidth/scrollWidth 都是 380，無橫向溢出。
- CSP 瀏覽器確認輸入錯誤提示、aria-invalid 與修正後儲存成功提示。Shell 桌面確認設定對話框 Escape 關閉及焦點回到設定按鈕。
- Shell 窄版／短視窗尚未完成瀏覽器實測；Modal 有元件回歸測試。未執行正式後端 browser-to-storage E2E。
- 臨時預覽頁已關閉，兩個 loopback Vite 服務已停止。

## 執行與證據

- 前端實作：Cursor Grok CLI，原生 session ba3bce00-e672-4f9f-a715-ada8d001a6cf；父代理檢查實際 diff、修正對比與測試、執行驗證。未宣稱獨立跨家族 release review 完成。
- JEV 用於候選優先序與最終範圍判斷。最終 jev1.13：符合簡約方向 0.92；足以稱完整 production E2E 0.03。以 >=0.8 / <=0.2 判讀，不作為測試證據。
- 測試與建置紀錄、CLI 產物：/tmp/anila-ui-polish-20260918/。
- 預覽截圖：/tmp/anila-ui-polish-20260918/shell-light.png、/tmp/anila-ui-polish-20260918/csp-light.png。
- /tmp 證據屬臨時檔，非已提交或長期保存的交付產物。
