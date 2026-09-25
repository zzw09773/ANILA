# pptx-renderer

> ANILA「產出中心」的**簡報渲染後端**：一個包住 [PptxGenJS](https://gitbrent.github.io/PptxGenJS/) + LibreOffice headless 的 Node HTTP 服務。anila-studio 的 slide 管線把結構化 spec 丟進來，這裡吐出真正的 `.pptx`、把 `.pptx` 轉成截圖、並做確定性的幾何品質檢查。純 server-to-server，不對一般使用者直接開放。

> English mirror：[`README.en.md`](./README.en.md)

> 🌿 **分支對照**：本服務跨 ANILA 部署分支內容一致。分支策略見根目錄 [`README.md`](../../README.md) 的分支對照表（現行單一 `main`；舊七分支模型已失效，見根目錄 README）。

---

## 在 monorepo 的位置（重構後 §17.1 版圖）

```
services/pptx-renderer/        ← 本服務（Node 22 + LibreOffice + Poppler，port 7100）
services/anila-studio/         ← 唯一上游呼叫者（產出中心引擎）
infra/compose/platform.yml     ← compose 定義（根目錄 compose.yaml 為 shim → include 它）
```

啟停走根目錄 compose shim：`compose.yaml` → `infra/compose/platform.yml`（project `anila`）。compose 中本服務只 `expose: 7100`、**不對 host 發佈 port**——它是 anila-studio 的內部渲染後端，不是需經 Service Registry / Launch Token 的「專案入口」GUI 服務。

---

## 端點（`server.js`）

| Method / Path | Body | 回應 |
|---|---|---|
| `GET /health` | — | `text/plain` `ok` |
| `POST /render` | `{ spec }` | `.pptx` 二進位（octet-stream） |
| `POST /screenshots` | `{ pptxPath \| pptxBase64 }` | `{ images: [{ index, mime, base64 }] }`（逐頁 PNG） |
| `POST /qa-geometric` | `{ pptxBase64 }` | `{ defects: [{ slide_index, ... }] }` |

- **`/render`**：`spec.slides` 必須為非空陣列（否則 400），超過 `MAX_SLIDES`（60）回 413。主題解析優先序 `spec.theme` → legacy `spec.palette` → 預設 `corporate_navy`（上游 CSP schema validator 會先把 palette 轉 theme，直接呼叫者仍可送 legacy palette）。
- **`/screenshots`**：走 LibreOffice headless（`.pptx` → PDF）→ Poppler（PDF → PNG）。給 `pptxPath` 時有 path-traversal 防護——只接受解析後落在 `TMP_ROOT` 底下的檔；或改給 `pptxBase64` inline。
- **`/qa-geometric`**：解壓 `.pptx`、逐頁解析 `ppt/slides/slideN.xml` 形狀幾何，回傳確定性的版面瑕疵清單（重疊 / 出血 / 空白區等），供 studio 的 vision-QA 迴圈使用（不需 LLM）。
- 監聽 `0.0.0.0:PORT`（`PORT` 預設 7100）。

> 輸入刻意 schema-light：上游（CSP / studio）已用 Pydantic 驗過結構才呼叫這裡；本服務只驗**會出事的東西**——payload 大小（`MAX_PAYLOAD` 10 MB）、slide 數上限、以及 `/screenshots` 的路徑穿越。

---

## 技術棧與相依

- runtime：Node **22**（`node:22-bookworm-slim`）。`package.json` 版本 `0.1.0`（name `anilalm-pptx-skill`）。
- 直接相依（`package.json`）：`express ^5.2.1`、`pptxgenjs ^3.12.0`、`react ^18.3.1` + `react-dom ^18.3.1` + `react-icons ^5.4.0`（供 `icons.js` 的 Heroicons resolver）、`sharp ^0.33.5`（點陣圖處理）。
- **vendored node_modules（air-gap 友善）**：本服務**刻意把 `node_modules` commit 進 git**（見 `.gitignore` 註解），讓內網 / air-gapped build 不必連 npm。Dockerfile 用 `npm ci --omit=dev` 從鎖定的 lockfile 裝。更新方式：`rm -rf node_modules package-lock.json && npm install` 後 commit。
- system 相依（Dockerfile）：`libreoffice-core` + `libreoffice-impress`（`.pptx` → PDF）、`poppler-utils`（PDF → PNG）、`fonts-noto-cjk` + `fonts-noto-cjk-extra`（CJK 字形，否則中文變 ????）、`tini`（PID-1 reaper，讓 SIGTERM 乾淨傳到 soffice 子行程，避免殭屍卡住 container restart）、`ca-certificates`。

---

## 結構

```
services/pptx-renderer/
├── Dockerfile          # node:22-bookworm-slim + libreoffice + poppler + noto-cjk + tini；npm ci --omit=dev
├── server.js           # Express app：/health /render /screenshots /qa-geometric（唯一入口）
├── icons.js            # concept → Heroicons PNG resolver（server.js 啟動即 require，缺它 → MODULE_NOT_FOUND）
├── package.json · package-lock.json
├── scripts/            # office 輔助（pack/unpack/validate/soffice、OOXML schemas、python thumbnail/add_slide/clean）
├── tests/              # 4 個 node smoke test（手動跑，見下）
├── SKILL.md · pptxgenjs.md · editing.md   # 產生 / 編修 pptx 的參考文件（container 內可 introspect）
└── LICENSE.txt
```

---

## 啟動與測試

```bash
# 在 stack 中（建議）— 根目錄 compose shim
docker compose up -d --build pptx-renderer      # → infra/compose/platform.yml

# 本機直接跑
cd services/pptx-renderer
npm ci --omit=dev        # 若沒有 vendored node_modules
node server.js           # 監聽 :7100（PORT 可覆寫）
curl http://localhost:7100/health   # → ok
```

### 測試（手動 node smoke test，非 CI 套件）

`tests/` 有 4 支 node 腳本，各自 `node tests/<file>.js` 執行，多數需要**一個在跑的 renderer**（server.js 或容器；以 `RENDERER_URL` 指定）：

| 測試 | 驗什麼 |
|---|---|
| `test_cover_hero_guard.js` | 只有 `image_gen_meta.use_case === 'cover_hero'` 才把圖升成標題頁滿版背景 |
| `test_hierarchy_bullets.js` | 階層項目符號（●/◦/▪ → indentLevel 0/1/2）解析正確 |
| `test_image_focus_render.js` | `image_data` 只在 `image_focus` 版面畫、`standard` 不畫 |
| `test_local_emptiness.js` | `findLargestEmptyRegion` 空白區分類（**inline 複製**受測函式，不需起 server） |

---

## 環境變數

| 變數 | 預設 | 說明 |
|---|---|---|
| `PORT` | `7100` | HTTP 監聽埠 |
| `PPTX_TMP_DIR` | 程式內 `/tmp/pptx-out`；Dockerfile 設 `/var/anila/pptx-out` | 暫存 `.pptx` / 轉檔工作目錄，同時作為 `/screenshots` path-traversal 白名單根 |
| `NODE_ENV` | `production`（Dockerfile） | Node 環境 |

---

## 與其他服務的關係

- **anila-studio（唯一上游）**：slide 管線 `studio_render.py` `POST {RENDERER_BASE_URL}/render` 取 `.pptx`；vision-QA 走 `/screenshots`；`geometric_qa.py` `POST /qa-geometric` 取瑕疵清單。`RENDERER_BASE_URL` 預設 `http://pptx-renderer:7100`。
- **無 DB、無 auth**：純內部 server-to-server；compose 不對 host 開 port，只由 stack 內的 anila-studio 走 docker 網路呼叫。它**不**參與 CSP 的 JWKS / 撤銷 / Task / Trace / 分類機制——那些都在上游 studio 完成後才把 spec 送進來。

> 重構脈絡：本服務是「產出中心」把簡報 spec 落地成檔案與截圖的渲染引擎；與 Slice 的關聯僅止於 §17.1 版圖與 compose shim。它**不是** Service Registry 的「專案入口」GUI 服務（那類需院內憑證卡 SSO + Launch Token + iframe policy 註冊）。

---

## 相關文件

- `SKILL.md` / `pptxgenjs.md` / `editing.md`：產生與編修 `.pptx` 的技術參考。
- 上游引擎：[`../anila-studio/README.md`](../anila-studio/README.md)
- 重構設計沿革（收斂紀錄）：[`../../docs/anila-redesign-docs/`](../../docs/anila-redesign-docs/)（`00-product-constitution.md` 憲章）。現行權威＝[`PLAN.md`](../../PLAN.md)（現況與執行順序）、規格＝[`SYSTEM-MAP.md`](../../SYSTEM-MAP.md)。
- 平台整體：[`../../README.md`](../../README.md) · 現行 `main`（舊七分支模型已失效）
