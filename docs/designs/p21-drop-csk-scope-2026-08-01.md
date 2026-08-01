# P2.1 拿掉 csk- 的工作範圍盤點（2026-08-01 全樹掃描）

> 擁有者 08-01 拍板方向：拿掉 `csk-` 靜態金鑰，agent 身分全面改短效簽章。
> 本文是開工前的範圍盤點（read-only 掃描結果＋工作分解），不是實作規格。
> 規格權威仍是 `SYSTEM-MAP.md:144-154`（5 分鐘 JWT、claims=`{user_id, department, agent_id}`、JWKS 驗簽）。

## 一、csk- 今天是什麼（一句話）

一把**雙向共用的長效對稱金鑰**：CSP 派工時放在 `X-CSP-Service-Token` 讓 agent 確認「來的是平台」；
agent 回頭打 CSP（RAG 搜尋、trace、artifacts、runtime-config、撤銷清單）時同一把當 Bearer 證明「我是哪個 agent」。
**使用者身分完全沒簽章**——`X-ANILA-User-Id/-Email/-Groups` 是明文，拿到金鑰或打得到 NodePort 的人可以冒充任何人。
這就是 SYSTEM-MAP 說「身分要能被證明，不能只是宣稱」的缺口，也是計費歸屬可信的前提（`SYSTEM-MAP.md:154`）。

## 二、可以直接沿用的既有基礎

- RS256 keypair＋`/.well-known/jwks.json`（`services/csp/app/api/jwks.py`）——**已上線，agent 端可達性 08-01 已在 Lab 實測**。
- 每請求簽發短效 token 的現成範本：`services/csp/app/modules/launch/token.py`（10 分/14 claims 的 launch token；
  PLAN 已記 D2/B4 決議「留著這套、P2.1 轉用、TTL 收斂到規格的 5 分」）。
- 兩份 production 級 JWKS 驗簽 client 可抄：`services/anila-studio/app/services/jwks_client.py`、
  `services/asr-gateway/app/services/jwks_client.py`（TTL 快取、unknown-kid 強制 refetch、背景刷新）。
- ⚠ agent 側（anila-core／anila-agent）**目前零 JWT 驗證程式碼**——這塊要新寫（抄上面）。

## 三、工作分解

### W0 環境水電（可先行，也是 e2e 驗收前提）
1. `cspki_ca_bundle.pem` 佈進 aiops Lab、agent CA 設定指過去（08-01 實測：Lab 缺的就這個）。
2. FQDN 申請（擁有者送單）；下來前 agent 端 `extra_hosts`（同 `.12` 前例）。
3. `.well-known` nginx 例外——本機已修（5898c49a），隨 P5.5 帶進 `.15`。

### W1 派工方向（CSP→agent）換簽章 —— SYSTEM-MAP 指定的核心
- CSP 派工時簽 5 分鐘 JWT（claims 照規格三件：`user_id, department, agent_id`；沿用 launch token 機制收斂 TTL 與 claims）。
- `build_agent_headers`（`services/csp/app/services/proxy/headers.py`）改：拿掉 `X-CSP-Service-Token` 與明文
  `X-ANILA-User-*`，身分全部進簽章 token。
- anila-core／anila-agent 範本補 JWT 驗簽 middleware（fail-closed）；
  ⚠ 一併殺掉舊 middleware 的 fail-open（`anila_core/api/middleware/auth.py:84`：空 token＝全放行）。

### W2 回程方向（agent→CSP）—— csk- 的第二個角色，容易被漏掉
- 任務內回呼（RAG 搜尋、trace span、artifacts）：直接複用該次派工 JWT（含 agent_id＋user＋task），
  CSP 驗簽後照舊做 `bound_collection_id` 範圍檢查。契約改動：search API 從 `Bearer csk-` 改吃 `Bearer <JWT>`。
- **任務外呼叫（runtime-config 輪詢、revocation feed）沒有派工 token 可用——唯一真正的設計決定**。三案：
  a) agent 註冊時登記公鑰、自簽短效 assertion 換 token（標準 private_key_jwt，最乾淨、多一段 PKI 管理）；
  b) 留一顆**低權限 poll-only** 長效 token（只能拉 config/撤銷清單，不能冒充使用者、不能搜資料——縮小版而非拿掉）；
  c) 拿掉輪詢改推送。開工時給兩案比較一頁，擁有者選。
- Router／worker 的 `service_clients` 同一套 csk- ——建議同步換，否則只拿掉一半。

### W3 發行／管理面拆除
- csk- 簽發、輪替、`bsk-` bootstrap、治理中心精靈 Step 2、guard snippets、CLI `agent bootstrap`——按 W2 選案拆除或改造。
- `agent_credentials` 的 AES 信封機制大幅縮水（或只留給 poll token）。

### W4 文件／範本／測試全面換（機械量大、風險低）
- 全樹 92 檔近 478 處引用；大頭在文件（bootstrap protocol 是 frozen 文件要改版）、developer guide、
  治理中心文案、範本 zip（anila-agent）、`.en.md` 孿生兩份、測試 fixtures。
- **這正是「零 agent 時改契約免費」的意思：沒有任何現場 `.env` 要遷移。**

### W5 驗收（行為級）
- 偽造 `X-ANILA-User-*` 打 agent → 拒收；改 JWT 任一 claim → 驗簽失敗；過期 → 拒收。
- JWKS 換鑰演練：`_serialize_jwks` 目前單鑰（rotation TODO）——P2.1 順帶改多鑰，否則第一次換鑰全斷。
- 計費歸屬抽查（SYSTEM-MAP:154：身分可偽造＝月報表不可信）。

## 四、開工前要先確認的兩件（掃描標 UNVERIFIED）
1. 活體 DB `select count(*) from agents / agent_credentials`——「改契約免費」建立在真的是零上。
2. 422 修復（f7f91b86）後 `anila-core register` 的真實 round-trip 沒實測過——開工先跑一次。

## 五、順帶發現
- `PLAN.md` 07-31 段寫「X.8 已修但刻意未合併」已過時——LRU 上限已在樹上（wt/leak 已於 07-31 合併，見 OWNER-QUESTIONS Q8）。
