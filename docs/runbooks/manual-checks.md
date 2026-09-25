# 手動三檢查 — 何時跑、怎麼跑

擁有者裁定：**不要 CI**（一人維運太辛苦）。本樹沒有 GitHub workflows；
`infra/ci/` 與 `infra/checks/` 裡的腳本是**你覺得該看的時候才跑**的工具，
不擋 commit、不擋 merge、不擋部署。

入口：

```bash
# 全部（結尾有三行摘要）
bash infra/checks/run-all.sh

# 單項
bash infra/checks/run-all.sh orm
bash infra/checks/run-all.sh contrast
bash infra/checks/run-all.sh zh
bash infra/checks/run-all.sh geometry
```

Exit code：`0` 乾淨、`3` 有發現（自己決定要不要修）、`1` 檢查壞了、`2` 用法錯。

依賴：只用專案／本機既有的 Python（標準庫 + 既有 venv 裡的
SQLAlchemy／alembic／psycopg2）。**不新增**要在內網主機另裝的套件。
ORM 檢查會借舊樹 `services/csp/.venv`（或設 `CHECKS_PYTHON`）；
scratch DB 預設連本機已在跑的 throwaway Postgres（`127.0.0.1:55441`，
可用 `SCRATCH_*` 覆寫）。**絕不**對 `anila-csp-db-1` 跑 alembic。
Check 4（幾何）同樣**不新增 npm／pip 相依**——用系統既有的 Chrome/Chromium；
🔴 **找不到瀏覽器時它回 BROKEN(1)，不回 PASS**（安靜通過的幾何檢查比沒有檢查更糟）。

---

## 1. ORM ↔ 真 Postgres schema

**何時跑**

- 改了 `services/csp/app/models/**`
- 新增／改寫 alembic migration（尤其 `r1_*`）
- 懷疑「程式以為有欄、庫沒有」或反過來
- 發版前若本月動過 schema，跑一次就夠

**做什麼**

對 scratch DB（`alembic upgrade head` 乾淨庫）比對 ORM metadata：
缺表／缺欄／型別明顯不符／可空性分歧／UNIQUE 背後缺索引／缺 FK；
另有 policy：`DateTime` 未宣告 `timezone=True`。

**不修什麼**

有發現 → 記進 PLAN，不要在檢查當下順手改 production schema。

⚠ **要先起 throwaway Postgres（`127.0.0.1:55441`），否則這一格恆 `BROKEN`**：

```bash
docker run -d --rm --name anila-scratch-pg -p 127.0.0.1:55441:5432 \
  -e POSTGRES_PASSWORD=x -e POSTGRES_USER=postgres pgvector/pgvector:pg16
# 用完
docker rm -f anila-scratch-pg
```

🔴 **映像必須帶 pgvector**。純 `postgres:16-alpine` 起得來、但 `alembic upgrade head` 會死在
`extension "vector" is not available` —— **那是另一種 BROKEN，不是修好了**（2026-08-24 我實際踩過才改的）。
🔴 **`BROKEN` 不是 `FINDINGS`**：前者是「檢查沒跑成」，後者才是「跑成了、有東西」——
**看到 BROKEN 先確認是不是 55441 沒起，不要當成 schema 有問題。**
📌 這一格 `run-all.sh` 現在會自己把上面那段指令印出來（跑不起來就要自己說出缺什麼）。

---

## 2. 對比色（WCAG AA）

**何時跑**

- 動了任一前端的色票／theme token（`anila-shell/index.html`、
  `csp-governance-ui/.../tokens.css`、`anilalm/.../tokens.ts`）
- 改了預設淺／深主題
- 發版前若本月動過顏色

**做什麼**

兩主題各算常見「文字色 × 背景色」對比，報 < 4.5:1 的配對。

**抓不到什麼（腳本自己會印）**

浮水印、半透明 overlay、漸層上的字、canvas／執行期改色——
今晚那種「淺底正常、深底淹死對話」只能渲染目視。靜態通過 ≠ 畫面安全。

---

## 3. 簡體／大陸用語（zh-TW）

**何時跑**

- 發版前（固定）
- 大範圍改前端文案或 API `detail=`／使用者可見錯誤字串之後

**做什麼**

`infra/ci/lint-zh-tw.sh` → 掃三前端 + `services/csp/app` +
`services/anila-studio/app`。命中可改用語，或（僅裝飾性）加 `zh-exempt`。

注意：「登錄」在台灣也可當「登記／註冊」用，詞表會把它當「登入」的
大陸用語打——若語境是登錄表／登記，屬誤報，自行判斷。

---

## 4. 燈箱／圖片幾何（需系統瀏覽器）

**何時跑**

不要維護一份元件清單（**清單不會自己長大**，第四個元件加進來時它不會更紅）。
驗收時問這一個問句：

> 🔴 **這個 diff 有沒有改到會被畫出來的東西？有的話，驗收單必須含 Check 4 的輸出。**

那是審查者本來就在做的判斷，不是要人記得的清單。

**為什麼測試回答不了這一格**

`apps/anila-shell` 跑 vitest + **jsdom**，**jsdom 沒有版面引擎**：
`getBoundingClientRect()` 全回 0、`elementFromPoint` 不做真正的命中判定。
`citedFigures.test.jsx` 對燈箱的斷言**全部是 style 字串**——守的是寫法，不是幾何。
2026-08-22 一天之內三條**使用者可見**的缺陷，**沒有一條是測試抓到的**：

| 缺陷 | 現象 |
|---|---|
| V-2 | letterbox 產生 **1535px 吞點擊死區**——直式圖左右各約 767px 看起來是暗底，點了沒反應 |
| R-1 | 修 V-2 用的 `pointer-events:none` **把圖上右鍵另存整個封掉**，而燈箱沒有其他存圖管道 |
| A6-1 | 修 R-1 之後，`onLoad` 前那一幀以自然尺寸渲染，**7.9 倍視窗寬**、捲不動 |

**做什麼**

四條不變式 × `4:1`／`1:4`／`1:1` × 視窗 `1920x1080`／`760x900`，另加 `box=auto` 那一幀：

1. **盒 == 繪製區**：圖外 1px 命中暗底且點得關；圖內 1px 命中圖本身
2. **長寬比 == 原圖**（任何視窗尺寸）
3. **任何時刻不超出視窗**，含 `onLoad` 前那一幀
4. **右鍵仍打得到圖**（`contextmenu` 的 target 是 `IMG` ⇒ 另存／複製圖片還在）

⚠ **極端長寬比是重點**——letterbox 面積在那裡最大，而**接近方形的圖在前提成立與否都會過**。

量測素材（`fitLightboxBox` 本體、backdrop／img 的 style）**從 `markdown.jsx` 抽出**再注入量測頁，
執行時會印出抽到的東西；**錨點漂掉時回 BROKEN 並說「抽取器要跟著改」，不會安靜地量一份過期的樣式。**

**抓不到什麼**

`fitLightboxBox` 的**算術**（高圖夾高、寬圖夾寬）是純函式，**jsdom 也算得出來**——
那一格屬於單元測試，不在這裡重複。**本支只回答「真的畫出來之後」的事。**

**驗這支檢查器自己（綠→紅→綠）**

```bash
infra/checks/run-all.sh geometry                       # 綠
cp apps/anila-shell/src/markdown.jsx /tmp/mutant.jsx   # 拿掉 fitLightboxBox 的高度約束
LIGHTBOX_SOURCE=/tmp/mutant.jsx infra/checks/check_lightbox_geometry.py   # 期望 exit 3
infra/checks/run-all.sh geometry                       # 綠
```

📌 `LIGHTBOX_SOURCE` 存在的理由：**驗檢查器自己時不必去動共用樹上的 `markdown.jsx`**
——別的席位可能正在那棵樹上跑套件。

2026-08-24 實測：未突變 `PASS`；上述突變 → `FINDINGS（3）`，三條都紅在**長寬比**那格
（`1:4` 歪 673%／`1:1` 歪 93%／小視窗 `1:4` 歪 274%），而 `4:1` **維持綠**
——**它是照形狀分辨的，不是全部一起紅。**

**誠實的殘餘**

`run-all.sh` 是手動的、不擋任何東西，**Check 4 仍然不會自己跑**。
它比一支孤兒腳本強的地方是「**休眠在一個有人會回去的地方**」，**不是「不再休眠」**。
📌 **Check 4 不是自動化幾何驗收的替代品，它是今天拿得到的那一半。**
**不可以因為「已經有 Check 4 了」就把那兩題永遠不問**
（內網建置機／出貨包帶不帶得動一顆瀏覽器；願不願意為這個部署釘死版本）。
