# 每日工作彙整 Routine 設定指南

> 目的：每天 **04:00（台北時間 UTC+8）** 自動彙整「前一日」我在 **GitHub（三個 repo）、Google Drive、Gmail** 的工作，並輸出成一份 **Google 文件**。

本檔由 Claude Code 產生，作為設定備份與日後維護用。實際排程需在網頁建立（原因見下）。

---

## 1. 為什麼要在網頁建立？

Claude Code on the web 的定時／週期性自動執行功能叫 **Routines**（文件：<https://code.claude.com/docs/en/routines>）。

- Routine = 一組「prompt + repo + 連接器 + 觸發條件」，存在你的 claude.ai 帳號裡，由 Anthropic 雲端在排定時間自動跑成一個完整的 Claude Code session（不需要開著電腦）。
- **限制**：在「web session 內」無法執行 `/schedule` 建立 routine，必須到網頁 UI 建立。所以這份設定無法由 session 內直接「按下建立」，需由你在網頁完成最後一步（約 2 分鐘）。
- 也可改用「本機終端機或桌面版 Claude Code」執行 `/schedule daily work summary at 4am`，效果相同。

前置條件：Pro / Max / Team / Enterprise 方案且已啟用 Claude Code on the web；GitHub、Google Drive、Gmail 連接器已連結（這些連接器流量走 Anthropic 伺服器，預設 Trusted 環境即可，不需額外開放網域）。

---

## 2. 建立步驟（網頁 UI）

1. 開啟 <https://claude.ai/code/routines> → **New routine**。
2. **Name**：`每日工作彙整`。
3. **Prompt**：貼上第 4 節〈Routine Prompt〉的全文；模型建議選 Claude 最新可用的 Opus 或 Sonnet。
4. **Repositories**：加入這三個 repo（每次執行會自動 clone）：
   - `zzw09773/anila`
   - `zzw09773/anila-agent`
   - `zzw09773/aiec_test`
5. **Environment**：用 **Default**（Trusted 網路）即可。
6. **Connectors**：確認包含 **GitHub、Google Drive、Gmail**；其餘不需要的可移除以縮小權限。
7. **Trigger → Schedule**：選 **Daily**，時間填 **04:00**，時區用你的本地時區（台北／Asia-Taipei，系統會自動換算）。
   - 註：實際執行可能因 stagger 晚幾分鐘，屬正常。
8. **Create**。建立後可在 routine 詳情頁按 **Run now** 立即試跑一次，確認結果無誤。

> 自訂間隔（例如非整點）可先選最接近的 preset，再用 CLI `/schedule update` 設定 cron。最小間隔為 1 小時。

---

## 3. 時間範圍說明

在 04:00（UTC+8）執行時，「前一日」= 台北時間前一個完整日曆日 00:00–23:59:59。Prompt 內以下列指令動態換算成 UTC 區間，供 GitHub 查詢使用，毋須手動改日期。

---

## 4. Routine Prompt（直接複製貼上）

```text
你是「每日工作彙整」助理。請彙整「前一日（Asia/Taipei 時區）」我在 GitHub、Google Drive、Gmail 上的工作，並輸出成一份 Google 文件。全程自動執行，不要詢問權限。

## 步驟 0：決定時間範圍
在 shell 執行以下指令，取得前一日日期與對應的 UTC 查詢區間：
```bash
YESTERDAY=$(TZ=Asia/Taipei date -d 'yesterday' +%Y-%m-%d)
SINCE_UTC=$(date -u -d "${YESTERDAY}T00:00:00+08:00" +%Y-%m-%dT%H:%M:%SZ)
UNTIL_UTC=$(date -u -d "${YESTERDAY}T23:59:59+08:00" +%Y-%m-%dT%H:%M:%SZ)
echo "彙整日期: $YESTERDAY (UTC 區間: $SINCE_UTC ~ $UNTIL_UTC)"
```
後續所有「前一日」皆以 $YESTERDAY 為準。

## 步驟 1：GitHub（zzw09773/anila、zzw09773/anila-agent、zzw09773/aiec_test）
- 三個 repo 已被 clone。對每個 repo 進入其工作目錄執行（涵蓋所有分支）：
```bash
git fetch --all --quiet
git log --all --since="$SINCE_UTC" --until="$UNTIL_UTC" \
  --pretty=format:'%h | %an | %ad | %s' --date=format-local:'%Y-%m-%d %H:%M'
```
  記下每個 commit 的 repo、短雜湊、作者、時間、訊息；以作者 zzw09773 為主，其餘標註作者。
- 用 GitHub 連接器查前一日的 PR / Issue 動態：
  - PR：對三個 repo 分別查詢 `repo:<owner/repo> updated:$YESTERDAY`，列出編號、標題、狀態（open / merged / closed）、連結。
  - Issue：以相同方式查 issue 的開啟 / 關閉 / 留言動態。

## 步驟 2：Google Drive
用 Google Drive 連接器找出前一日（modifiedTime 落在 $YESTERDAY）有建立或修改的檔案，記下檔名、類型、最後修改時間、連結。

## 步驟 3：Gmail
用 Gmail 連接器搜尋前一日的重要信件（搜尋字串約為 `after:YYYY/MM/DD before:(隔天) YYYY/MM/DD`）。摘要寄件者、主旨與重點；略過廣告／系統通知類。

## 步驟 4：撰寫彙整（繁體中文）
依下列結構整理；某段若無資料寫「（無）」：
# 工作彙整 — {YESTERDAY}
## 一、重點摘要（3–5 條）
## 二、GitHub
### Commits（依 repo 分組）
### PR / Issue 動態
## 三、Google Drive 檔案異動
## 四、Gmail 重要往來
## 五、待辦 / 後續（如有）

## 步驟 5：輸出成 Google 文件
用 Google Drive 連接器建立一份新的 Google 文件：
- 標題：`工作彙整 {YESTERDAY}`
- 內容：步驟 4 的彙整全文（以 text/plain 上傳並轉成 Google Doc）。
建立後，在本次 session 回覆中附上該文件標題與連結，並把彙整全文一併貼出。

## 容錯
任一來源查無資料或連接器暫時不可用時，於該段註明原因即可，不要中斷整體彙整。
```

---

## 5. 注意事項

- **執行結果在哪看**：每次排程會建立一個新 session，可在 <https://claude.ai/code> 或手機 App 看到完整過程；產出的 Google 文件連結也會貼在該 session 回覆中。
- **綠燈 ≠ 成功**：run 清單的綠色只代表 session 正常啟動結束，不代表任務成功；首次設定後請開 run 確認 Google 文件確實建立。
- **用量**：routine 會計入訂閱用量與「每日 routine 執行上限」。
- **想改設定**：在 routine 詳情頁按鉛筆編輯 prompt / repo / 連接器 / 排程；用 **Repeats** 切換暫停或恢復。
- **想另外寄 Email 通知**：可在 prompt 步驟 5 後加一句「並用 Gmail 連接器把文件連結寄到 kunggemini09773@gmail.com」。
