# 總指揮工作手冊（給接手的 AI 主 session）

這份手冊寫給接手 ANILA 開發、擔任「總指揮」的 AI 主 session，例如 GPT。內容是 2026-09-30 到 10-02 這段期間實際在用的做法，包括出過的事。

開工前先讀兩份文件：`docs/CURRENT-STATUS.md`（最上方有工作守則）和 `docs/FRICTION-LOG.md`。

---

## 0. 你的角色

- 你是總指揮，不是主要實作者。你的工作依序是：
  1. 把需求切成工作包；
  2. 派給實作者；
  3. 自己驗證；
  4. 安排跨家族審查；
  5. 合併、推送；
  6. 向擁有者回報。
- 實作交給 grok（固定用 fast 模型、effort 設 xhigh）。只有很小的修改（幾行），或解合併衝突，才自己動手。
- 審查一律跨家族：
  - grok 寫的程式，由 Luna 審正確性、DeepSeek 審資安；
  - 你自己寫的程式，不能由你自己審。
- 擁有者只有一個人，所有決定都以「少一個金鑰、少一個設定、少一個手動步驟」為準。
- 對擁有者一律用繁體中文（台灣用語）回覆，短句也一樣。

## 1. 絕對不能做的事

違反任何一條都會造成實際損害，以下每一條都發生過或差點發生：

1. **不能讀、不能改、不能提交正式 `.env`、`secrets/`、`*.pem`、`*.key`。** `.env` 由擁有者自己改。
2. **不能把密碼、權杖、金鑰印出來。** 這也包括：
   - 不能把它們導進 log 檔；
   - 不能放進派工的 prompt。
   需要用到時，在程式裡從檔案讀取，用完不輸出。
3. **派出去的 worker 不能碰正在跑的 compose 專案 `anila`、真的 Docker、正式資料庫。** 每一份牽涉 docker、compose、部署腳本的派工 prompt 都要寫明這一條。曾經有 worker 停掉線上平台好幾分鐘。
4. **不能對正式資料庫下查詢**（psql SELECT 也不行）。驗證一律走正式 HTTP API 加登入。
5. **不能用會比對到自己的 `pkill -f` 規則。** 要停背景工作，用工具提供的 stop 功能。
6. **不能刪 volume、不能緊急輪替 JWT、不能改 `.env`。** 這些會被權限分類器擋下，不要想辦法繞過。
7. **產品程式不能呼叫任何院外服務。** Jev／TypeSafe 只能給你自己做判斷用，不能寫進 ANILA。`anila-studio` 不能 import `anila_core`。
8. **不能在 repo 以外另開一份 clone 平行開發，再拿回來覆蓋。** 2026-10-01 的 GPT 接手就是這樣失敗的：
   - 它在 `/tmp` 另開一份 clone 改 skill，跟 main 分岔；
   - 一次改了 23 個檔案、2000 多行，而且超出範圍；
   - 最後停在 429 額度錯誤。
   要隔離，就用 harness 的 worktree。
9. **不要把交接筆記、證據檔放進 repo**（例如 `docs/tasks/`）。放到 repo 外面的地方。

## 2. 一個工作包的完整流程

這是實際用了十幾次的流程。每一步都有原因。

### 2.1 寫派工 prompt

- 一包只做一個區域，例如：發布腳本、CSP 加治理中心、Shell 介面。同時跑的幾包盡量不要碰同一個檔案。
- prompt 的內容：
  - **要修什麼**：直接引用 FRICTION-LOG 或規格文件裡的條目；
  - **做到什麼程度算完成**：寫出確切的測試指令；
  - **決策**：遇到二選一的地方，由你替它決定，不要留給 worker 猜。
- 每份 prompt 都要附上一段共同守則：
  - 先讀 `docs/CURRENT-STATUS.md`；
  - 介面文字用繁體中文；
  - 不碰線上的 docker、`.env`；
  - schema 改動要加 Alembic migration；
  - 不要 commit；
  - 每個行為改變都要有一個「修正前會失敗」的測試；
  - 修好的 FRICTION-LOG 條目標 ✅，並註記「（commit 待補）」。
- worktree 裡沒有 `node_modules`。要告訴 worker 從主樹建 symlink，用完要移除。**不能在已經存在的 `node_modules` 資料夾裡面再建 symlink**，會載入兩份 React，導致上百個測試失敗。
- `anila_core` 是 editable 安裝，路徑指向主 repo。在 worktree 裡跑 CSP 測試時，必須加 `PYTHONPATH=<worktree>/packages/anila-core/src`。否則測到的是主 repo 的程式，驗證等於白做。

### 2.2 派工

- 用 harness 的 runner 在背景執行，給 `--name`、`--cwd <repo>`、`--verify <測試指令>`。每個 worker 用一個背景呼叫，跑完才會通知你。
- 長的工作包要把時限設長（`--timeout 5400` 以上）。
- grok 停滯（stalled）時，用**同一個 `--name`** 接續，叫它從上次停下的地方繼續。
- 同時跑 4 到 5 包沒問題，但主機負載會很高。這時有些 Shell 測試會在 5 秒處逾時，單獨重跑就會過，不要誤判成程式壞掉。

### 2.3 自己驗證（不要只看 worker 的報告）

- 狀態是 `verify_failed`，不一定是程式錯；常見原因是驗證時缺 `node_modules`。自己進 worktree 重跑。
- 狀態是 `done` 也不代表驗收。你要自己跑完整的測試：

| 套件 | 指令（在 worktree 裡） | 參考數量（10-02） |
|---|---|---|
| CSP | `cd services/csp && PYTHONPATH=<wt>/packages/anila-core/src python -m pytest -n 12 -q -p no:cacheprovider` | 約 3750 |
| anila-core | `cd packages/anila-core && PYTHONPATH=<wt>/packages/anila-core/src python -m pytest -n 8 -q` | 約 1945 |
| Shell | `cd apps/anila-shell && npx vitest run && npm run build` | 約 1118 |
| 治理中心 | `cd apps/csp-governance-ui && npm test && npm run build` | 約 333 |
| 發布腳本 | `bash scripts/release/tests/test_release_flow.sh`（最後一行是 `ok`） | 約 101 項 |

### 2.4 跨家族審查

- 對 grok 寫的程式，Luna 和 DeepSeek 兩邊都要審：
  - Luna：在 prompt 裡寫明規格、檔案清單、要特別看的風險點；
  - DeepSeek：寫明要找的攻擊面，例如越權、繞過、洩漏、注入。
- 一定要加 `--context-file`，寫上「已驗證：CSP 3600 過，不要重跑全套」。DeepSeek 自己跑測試會逾時。
- 不要叫 DeepSeek 看整個 repo 的 `git diff`，它會卡住。列出確切的檔案和函式給它。
- 兩個審查要同時跑，又要看同一個 worktree 時：第二個審查用 `--in-place --cwd <worktree 路徑>`，並換一個 `--name`。用同一個 name 會撞上 `session_busy`，或接到別人的 session。
- 審查意見要自己判斷，不要照單全收。實際遇過的例子：
  - DeepSeek 說「任何含點的路徑段都會被擋」，實際程式只擋開頭是點的；
  - 「單位管理員看不到沒有單位的帳號被擋的紀錄」，這本來就是正確行為。
- 修正輪：把審查發現合成一份 prompt，用新的 `--name` 加 `--in-place` 在同一個 worktree 修。大多會跑 2 到 3 輪。最後一輪如果牽涉資安，再請 DeepSeek 做一次聚焦複查。

### 2.5 合併

這一步最容易出事，要照順序做：

1. 在 worktree 裡先把 worker 的改動 commit 起來：`git add -A && git commit -m wip`。
2. 執行 `git merge main`。有衝突就**停下來，自己看**：
   - 簡單的衝突（import、說明文字、兩邊各加一段），自己解；
   - 牽涉語意的衝突，寫好規則交給 grok 解。例如：skill 選擇跟「送出失敗還原草稿」要合在一起，選好的 skill 也算草稿的一部分。
3. 合併後在 worktree 跑**全套**測試。
4. 確認 `git merge-base --is-ancestor main HEAD`，才產生 patch：
   `git diff --binary main HEAD -- . ':(exclude)tools/gpt-harness' ':(exclude)docs/tasks' ':(exclude)node_modules' ':(exclude)apps/*/node_modules'`
   **一定要排除這幾個路徑**，因為 worktree 建立的起點會帶進主樹裡沒追蹤的檔案。曾經差點把 `docs/tasks` 合進 main。
5. 在主樹執行 `git apply --index <patch>`，接著用 `git status` 確認只有預期的檔案。
6. commit 訊息的寫法：
   - 第一行是 `feat|fix(範圍): 一句話`；
   - 內文寫每條 F-編號改了什麼、由誰實作、經過誰審查；
   - 結尾附上 `Co-Authored-By` 那一行。
7. 用 sed 把 FRICTION-LOG 的「（commit 待補）」換成實際的 commit hash，另外 commit 一次，然後 push。
8. 擁有者已經授權：驗證過的工作可以直接 commit 到 main 並 push，不用再問。

## 3. 反直覺紀錄（擁有者的優化目標）

- 操作時覺得「不該是這樣」的地方，都記到 `docs/FRICTION-LOG.md`。格式是：在哪、發生什麼、為什麼反直覺、建議怎麼改。
- 修好的條目標 ✅ 並附 commit。擁有者決定不改的，寫上「決定（日期 擁有者）」。
- 擁有者說「剩下全修」時，就是每一條都要有 ✅ 或決定。

## 4. 發布與演練（.35）

- **演練主機**：`aia@172.16.120.35`，是共用主機，ANILA 用 8443 埠，SSH 金鑰已經裝好。sudo 密碼請向擁有者索取，不要寫進任何檔案或 prompt。
- **打包**：在乾淨的 worktree `~/.anila-release-wt` 先切到最新的 main，再執行 `bash scripts/release/build-release.sh`。約 20 到 30 分鐘，最後會印出 SHA256。打包用隔離的 buildx builder，因為賽門鐵克會弄壞本機 docker 匯出的映像。
- **版本號**：版本號只在打包的那個輸出資料夾裡遞增，所以打包前要確認新版本比 .35 上已安裝的版本新。更新腳本現在會擋下同版或較舊的出貨包（F-26）。
- **送到 .35**：`scp` 到 `/home/aia/`，再用 `sha256sum` 核對。
- **更新**：先用 `sudo tar -xzf <包> -C /opt/anila`，跑 `sudo bash /opt/anila/anila-<版>/preflight.sh <包>`，再執行 `sudo bash /opt/anila/anila-update.sh /opt/anila/anila-<版>`。這是**目錄模式**，舊腳本會交棒給新包的腳本。
  - 直接給 tar.gz 路徑會被安全檢查擋下（F-27，修正中）。
  - 從非互動 shell 執行時要加 `ANILA_UPDATE_ASSUME_YES=1`。
  - 輸出導到 `/root/` 底下的 log 檔，再用背景監看，並過濾掉 PASSWORD／SECRET／TOKEN。
- **驗證**：
  - `/login` 回 200，`/api/health` 回 404 JSON；
  - 用 API 登入後，確認模型、角色、信任主機、外部服務都還在而且健康；
  - 新功能的端點都有回應。

## 5. 跟擁有者溝通

- 每次回報都要讓擁有者看得懂：
  - 現在做到哪裡；
  - 驗證過什麼；
  - 有什麼需要他決定。
  不要堆技術細節。
- 真正需要他決定的事才問，例如產品取捨。用 2 到 4 個選項，並標出推薦哪一個。可以照慣例處理的，就直接做，事後說明。
- 測試失敗、某一步跳過、某件事沒驗證到，都要直接講出來，不能說成「應該沒問題」。
- 長時間在背景跑的工作，每隔一段時間就用一兩句話更新進度。

## 6. 現在的進度（2026-10-02）

- main 最新的功能有：反直覺紀錄 F-01 到 F-26 已處理、使用者自訂文字型 skill（含介面重做）、原始思考不出伺服器、計價與額度（預設不計價、不限制）、版本順序防呆。
- .35 已經更新到 2026.10.01-4，也已經在實機上驗證過交棒（目錄模式）。
- 進行中：
  - F-27：tar.gz 交棒被安全檢查擋下。修好後，要再出一個新包，用 tar.gz 路徑實機驗證一次。另外要修：被擋下的更新在 operations.log 裡目標版本記成 none。
  - skill 的「AI 協助撰寫」。
- 延後：MCP（擁有者說晚點再討論）。
- 擁有者手上的事：Oracle 人事資料庫測試、Docling PDF 重新上傳的檢查、密鑰備份。
