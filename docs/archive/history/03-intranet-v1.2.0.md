> ⚠ 2026-08-17 盤點：本檔為【歷史紀錄】(專案起源史),不代表現況。現行狀態與執行順序見 `PLAN.md`。

> ⚠ **2026-08-01 P2.1**：agent 派工身分已改為平台現簽的 5 分鐘 JWT（JWKS 驗簽；
> 開發者不領 `csk-`／`CSP_SERVICE_TOKEN`）。下文保留當時紀錄，**勿依此做現行接入**；
> 現行上手見 `docs/guides/developer-guide.md` 與治理中心「接入驗簽 · 三級制」。

# 時代 C：prod-intranet-card 線與 intranet v1.2.0 部署

## 概述

本時代把 ANILA 從「外網開發中的平台」收斂成「可帶進中科院內網 air-gapped 環境、插自然人憑證卡即登入」的可交付堆疊。主線是 `prod-intranet-card` 分支：它在 `main` 之上疊加卡登認證與內網部署 delta，並連下三個版號標籤 `v1.0.0`（2026-06-14）、`v1.1.0`（2026-06-22）、`v1.2.0`（2026-06-23）。

依 git 範圍界定，本篇 commit 對照表涵蓋 `v1.1.0..v1.2.0`（1 個）與標籤後 `v1.2.0..origin/prod-intranet-card`（6 個）共 **7 個** in-range commit。但這條線的實質里程碑在 v1.1.0 之前就已奠基——卡登真驗章重寫、CSPKI 信任鏈、離線工具鏈、JWT 金鑰部署修復——因此本篇一併敘事並標明這些前置 commit（範圍外，供追溯），再帶到 v1.2.0 的員編下傳與標籤後的部署收尾與 hardening。

主要事實依據：git commit 訊息與 tag 註解、`docs/runbooks/intranet-deployment-runbook.md`、`infra/deployment/intranet/` 工具鏈腳本標頭。

---

## 里程碑敘事

### 1. 卡登 PKCS#7 / CMS 真驗章重寫（前置：`ed13e5c`，2026-06-12）

這是整條內網線的安全地基，也是一次 CRITICAL 認證繞過的修復。改寫前，`/api/auth/card/verify` 只用 `load_der_pkcs7_certificates` **解析** PKCS#7、從不**驗章**——任何人都能自簽一張把 `serialNumber` 設成任意員工編號（甚至 owner `1147259`）的憑證 POST 進來，換到一個完整的 owner session。內網唯一登入路徑等於形同虛設。

`ed13e5c` 實作了真正的伺服器端驗證，並釘死到中科院 CSPKI 信任錨：

- **CMS 簽章驗證**：驗 SignerInfo 對 signedAttrs 的簽章，且 `messageDigest == hash(eContent)`。
- **憑證鏈驗證**：鏈到釘死的 CSPKI Root（`CSPKI Root Certification Authority - G1` 經 `中科院憑證管理中心 - G1`），含有效期窗檢查；自簽偽造無法鏈到根 → 拒絕。
- **nonce 綁定**：`eContent` 必須等於挑戰 nonce（防重放），從 `card_auth_service.decode_card_challenge` 串到 `verify_pkcs7_signature`。
- 對不可信 DER 的任何非預期解析錯誤一律回 **401**，不外洩成 500。

CA bundle 隨碼附帶於 `app/services/cspki_ca_bundle.pem`（可用 `CARD_CA_BUNDLE_PATH` 覆寫）。線上撤銷（CRL/OCSP）刻意不做——離職靠實體回收/銷卡加伺服器端 `User.is_active` 把關，是明確的 air-gap 取捨。新增純 Python 的 `asn1crypto==1.5.1` 做 CMS 解析（cryptography 41 無 CMS verify）；以真實測試卡 fixture 驗證：有效簽章解出員編、偽造／錯 nonce／竄改全數拒絕，單元測試重寫 16 綠。

### 2. CSPKI TLS 信任鏈（前置：`22c9936` = v1.0.0；`6eadd83`）

內網一切 https 都走中科院 CSPKI：`CSPKI Root CA G1`（自簽 root）→ `中科院憑證管理中心 G1`（中繼）→ leaf。兩個關鍵修正：

- `22c9936`（2026-06-14，即 `v1.0.0` 標籤所在）：從 `server.pfx`（空密碼）**抽出完整 TLS 鏈（leaf + 中繼）**，而非只抽 leaf——否則用戶端驗不到中繼會斷鏈。
- `6eadd83`（2026-06-22）：`intranet-deploy.sh` 的 `[2/7]` **預設把 model-CA 指向 `cspki_ca_bundle.pem`**，並**守衛空/壞 CA 檔**。這一步的洞察是：驗「卡片簽章」與驗「.12 模型 gateway 的 TLS」其實是同一套 CSPKI CA（內網 PKI 卡與伺服器憑證同源），所以卡登那份 bundle 正好就是 csp 信任 `.12` 所需的完整鏈，免下載、離線即有。守衛的必要性在於 `SSL_CERT_FILE`（`ANILA_MODEL_CA_FILE`）是**取代**整個系統信任庫而非疊加——指到空檔會讓 csp 所有出向 https 全部 `CERTIFICATE_VERIFY_FAILED`。

### 3. intranet 離線工具鏈（前置：`c4f4e8f`、`966d521`、`d63f706`）

把「外網 dev 機建置 → 帶進無外網內網一鍵跑起來」做成腳本，全部落在 `infra/deployment/intranet/`：

- **`build-and-export-for-intranet.sh`**：把整套 stack（csp / anilalm / anila-ui / ingestion-worker / router / pptx-renderer + 3 base + 3 cold-service + 4 model）build 後 `save` 成 tar.gz 分片（`01-anila-built` / `02-base` / `03-cold` / `04-models` / `05-weights-*`），連同 `INTRANET-LOAD.sh`（含 sha256 驗檔）、`MANIFEST.txt`、`CHECKSUMS.sha256`。`WITH_MODELS` / `WITH_WEIGHTS` 旗標決定是否連 model image 與 HF 權重一起帶（內網無下載通道，權重只能從這裡帶）。
- **`pack-chunks.sh` / `unpack-chunks.sh`**：把大檔或目錄切成 ≤45GiB chunk（給「50G 轉入通道」用），邊切邊算逐 chunk sha256。
- **`intranet-quantize-nvfp4.py`**：內網離線 NVFP4 量化腳本（B200 換裝後用；H100 跑不了 NVFP4，屬前瞻工具）。
- **`intranet-deploy.sh`**（`c4f4e8f`，2026-06-14 的一鍵互動式部署）：跑完 TLS 抽取 → 模型 CA → 產 `.env`（自動生 secret、問 gateway key / owner 工號）→ load image → JWT 金鑰 → `up` → 驗證；重跑安全（偵測既有 `.env` 保留 secret，不重生 DB 密碼）。第一版採 **gateway-only**：模型走 `.12` gateway，不在平台主機跑本地權重，`GEMMA4_BASE_URL` / `FLUX_AGENT_BASE_URL` 留空由 auto_seed 跳過。

`966d521`、`d63f706`（2026-06-12）補上 intranet-card 專屬的 docker-compose + nginx，以及讀卡機/HiPKI 故障時的 card-only owner break-glass 後路。

### 4. JWT keypair 部署修復（前置：`cd34e0b`，2026-06-15）

2026-06-15 live 預演抓到的部署阻斷：csp 用 RSA 私鑰簽登入 access token 並對 anila-studio 等發 JWKS，但 prod 模式 `ALLOW_AUTO_KEYGEN=false` 不自動生金鑰；缺這把 → `/.well-known/jwks.json` 回 500、登入發不了 token、anila-studio crash-loop。`cd34e0b` 讓 `intranet-deploy.sh` 的 `[4b]` 步驟用 csp image 跑 `generate-jwt-keypair.py` 產 `secrets/jwt-{private,public}.pem`，compose 以 `:ro` mount 進 csp；`*.pem` 已被 `.gitignore` 擋，不進公開 repo。

### 5. 員編下傳 + D8 移除（`80f60d4` = v1.2.0，2026-06-23，**in-range**）

v1.2.0 的定版 commit。把**員工編號（員編）下傳**到 chat / embedding 呼叫的下游（同時給 agents 與模型 gateway），在 `X-ANILA-User-Id` 中**以員編取代 DB primary key**，讓對話能歸戶到人、agent 能代使用者對院內系統動作。以**目的地驅動的 header builder**確保 CSP service token 不會外洩到模型 gateway；usage 統計仍以 DB PK 為鍵。

同時移除 **D8 confused-deputy**：agent 端可跨租戶拉他人 memory-facts 的舊路徑（任一 agent token 都能讀任意使用者的 facts）被刪除，連同已無用的 `anila-core` HTTP client（`http_user_facts.py` 等）——因為 CSP 已改成把記憶 push-inject 進 Router，這條 pull 既冗餘又危險。設計文件見 `docs/.../2026-06-23-employee-id-downstream-design.md`，Codex 三輪審過（design + impl + final: SAFE TO COMMIT）。

### 6. v1.2.0 標籤 + `.15` 主機內網部署

`v1.2.0`（annotated tag `7b9ba58` → commit `80f60d4`）標記於 prod-intranet-card。內網部署目標拓撲（見 runbook）：

- **平台主機 `10.53.100.15`**（對外名稱 `anila.ai.ncsist.org.tw`）：docker compose 全棧，nginx :443 → csp（FastAPI），下接 postgres + redis + ingestion-worker。
- **模型主機 `10.53.100.12`**（`aiagent2.ai.ncsist.org.tw`，My-OpenAI-Frontend gateway）：csp 走 https（CSPKI CA 驗證）+ `MODEL_GATEWAY_API_KEY`（Bearer）出向 `gpt-oss-20b` / `nv-embed-v2`，模型容器不對外開 port。
- 入向信任邊界：卡片硬體 + PIN + HiPKI 簽出 PKCS#7 → backend 真驗證（里程碑 1）→ 過關才抽 `serialNumber`。

部署後兩件營運必做（預演 critic 抓到）：owner 首登後先建 department（否則同仁註冊時單位下拉為空），以及保留讀卡機/HiPKI 故障時的 break-glass 途徑。

### 7. Router 記憶個人化（`c591c38`，2026-06-23，**in-range**）

ANILA Router 開始依使用者長期記憶與偏好重組回覆。直答／澄清類回覆由新的 `_ROUTER_SYSTEM_TEMPLATE` 規則要求 router LLM 依偏好調整語氣/語言/詳略/格式（CSP 已在 routing 呼叫注入記憶，不需額外 LLM pass）；dispatched 回覆則經一次 `_recompose_reply` 重組（記憶同樣由 CSP 注入該呼叫，維持 User → Router → CSP 拓撲，Router 本身不抽取也不轉傳記憶）。不可信的 agent 回覆被 sanitize 後當「資料」包裝而非「指令」；出錯/逾時 fail-safe 回原文，classified 回覆略過重組照原樣串流。始終開啟、無旗標。

### 8. 同源 `/anila` 入口（`401a15d` 前置 → `a06c0cb`，2026-06-25，**in-range**）

先前 `401a15d`（2026-06-09）把 anila-ui 從獨立 port `:4443` 改成與 CSP admin 同源的 subpath，讓平台內免重登 SSO 成立（同 host cookie 共用）、link card 可用相對路徑；後續此入口以 `/anila` 對外提供。`a06c0cb`（in-range）把 nginx `location /anila` 收緊成 trailing-slash 前綴 `/anila/`（搭配 exact `= /anila` 301），避免 broad-match 誤攔；因本分支 `/anila` 位置與 main 不同，cherry-pick 會 mis-anchor 故採 manual apply，4443 legacy redirect shim 的裸 `/anila` catch-all 刻意不動。

### 9. 標籤後 hardening 群（in-range）

`v1.2.0` 標籤後 6 個 commit 除了里程碑 7、8，還有 4 個內網實戰 hardening：

- `7c0ccd0`：ANILALM 串流聊天用裸 fetch 打 `/v1/chat/completions` 繞過 axios client、漏掉 CSRF header；卡登/SSO 下 ANILALM 只有 cookie（in-memory token 未填），mutating POST 被 `CsrfMiddleware` 403。改由共用 `csrfHeader()` 回送 `anila_csrf` cookie 為 `X-CSRF-Token`。
- `cb7d23e`：`MEMORY_LLM_MODEL` 預設 gemma4，但 air-gapped 部署只註冊 gpt-oss → fact extraction 靜默失效（使用者說的事實從沒被記住）。改成 `_resolve_extraction_target()` 在設定模型未註冊時 fallback 到第一個 active 的 registry LLM。
- `2638280`：CSP 原以 `issued_at DESC` 選 agent 出向 `csk-`，但 rotate 只更新 `rotated_at`——輪替非最新那把會產生 CSP 不會出示的 token，被 fail-closed guard 403。改以 `coalesce(rotated_at, issued_at) DESC, id DESC` 決定性排序，並讓 test-connection 探針用同一把。
- `40d6bba`：發 `csk-` 時附上可複製、零相依的 fail-closed FastAPI 入向 guard 片段（驗 `X-CSP-Service-Token`）給無法 import `anila_core` 的非模板 agent，並加「CSP 派送中」badge 標示平台實際派送的那把憑證。

---

## 完整 commit 對照表

### `v1.1.0..v1.2.0`（1 個）

| hash | 日期 | 摘要 |
|---|---|---|
| `80f60d4` | 2026-06-23 | feat(proxy)：員編下傳給 agents/模型 gateway（取代 DB PK）+ 移除 D8 跨租戶記憶 pull 與死 client（= v1.2.0 標籤）|

### `v1.2.0..origin/prod-intranet-card`（標籤後 6 個）

| hash | 日期 | 摘要 |
|---|---|---|
| `c591c38` | 2026-06-23 | feat(router)：依使用者記憶個人化回覆（直答 inline + dispatched recompose）|
| `7c0ccd0` | 2026-06-24 | fix(anilalm)：cookie 認證聊天補送 `X-CSRF-Token`（修卡登後 403）|
| `cb7d23e` | 2026-06-24 | fix(memory)：fact extraction 在設定模型未註冊時 fallback 到可用 LLM |
| `2638280` | 2026-06-24 | fix(credentials)：派送最近 issued-or-rotated 的憑證（coalesce 排序）|
| `40d6bba` | 2026-06-24 | feat(agents)：`csk-` 入向 guard onboarding 片段 + 「CSP 派送中」badge |
| `a06c0cb` | 2026-06-25 | fix(nginx)：`/anila` 收緊為 trailing-slash 前綴（去掉 broad match）|

### 里程碑前置 commit（範圍外，供追溯）

| hash | 日期 | 摘要 |
|---|---|---|
| `ed13e5c` | 2026-06-12 | fix(security)：卡登真 PKCS#7/CMS 驗章（stub→真驗證，關認證繞過 CRITICAL）|
| `966d521` | 2026-06-12 | chore(config)：intranet-card 的 docker-compose + nginx |
| `d63f706` | 2026-06-12 | feat(intranet)：部署整備 + card-only owner break-glass |
| `c4f4e8f` | 2026-06-14 | feat(deploy)：一鍵互動式 `intranet-deploy.sh` |
| `22c9936` | 2026-06-14 | fix(deploy)：從 `server.pfx` 抽完整 TLS 鏈（leaf+中繼）（= v1.0.0 標籤）|
| `cd34e0b` | 2026-06-15 | fix(intranet-deploy)：產 JWT 簽章金鑰 + 補 live 預演部署缺口 |
| `6eadd83` | 2026-06-22 | feat(intranet-deploy)：model-CA 預設 CSPKI bundle + 守空 CA 檔 |
| `f3d3550` | 2026-06-22 | fix(ingestion/usage)：6 項內網回報問題 + review hardening（= v1.1.0 標籤）|

---

## 本時代結束時的系統樣貌

到 `a06c0cb`（2026-06-25）為止，`prod-intranet-card` 已是一條可交付的 air-gapped 部署線：卡登走釘死到 CSPKI 的真 PKCS#7/CMS 驗章，出向模型走 `.12` gateway 的 CSPKI TLS（同一套 CA），一鍵 `intranet-deploy.sh` 加離線 image/權重/切塊工具鏈把整棧帶進 `.15` 主機，JWT 金鑰在部署時自動 provision。功能面上，員編已能歸戶下傳、D8 跨租戶記憶洞已補、Router 依記憶個人化回覆、ANILA runtime UI 以同源 `/anila` 免重登進入，標籤後又補齊 CSRF、記憶 LLM fallback、憑證派送排序、`csk-` 入向 guard 等內網實戰 hardening；版號定於 `v1.2.0`。

依 CLAUDE.md §5 部署狀態，收尾唯一待辦落在 user 端：`.12` gateway 的 `MODEL_GATEWAY_API_KEY` 簽發，以及 DNS 由 IP 過渡到 FQDN。第一版採 gateway-only（不跑本地權重），本地 gemma4/FLUX 待權重到位再開，架構不變。
