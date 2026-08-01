# P2.1 拿掉 csk-：範圍與思路（2026-08-01 拍板）

> 擁有者 08-01 拍板方向：拿掉 `csk-` 靜態金鑰，agent 身分全面改短效簽章。
> 規格權威＝`SYSTEM-MAP.md:144-154`（5 分鐘 JWT、claims=`{user_id, department, agent_id}`、JWKS 驗簽）。
> 本文＝開工前的全樹盤點＋問答收斂的設計原則；細部實作規格開工時另立。

## 一、為什麼換（現況一句話）

`csk-` 是一把**雙向共用的長效對稱金鑰**：平台派工時用它證明「我是平台」（`X-CSP-Service-Token`），
agent 回頭打平台（RAG 搜尋、trace、artifacts、runtime-config、撤銷清單）用同一把證明「我是哪個 agent」。
**真正的洞不是金鑰本身，是使用者身分完全沒簽章**——`X-ANILA-User-Id/-Email/-Groups` 是明文旁跟，
拿到金鑰或打得到 agent NodePort 的人可以冒充任何使用者。
這就是 SYSTEM-MAP「身分要能被證明，不能只是宣稱」的缺口；`SYSTEM-MAP.md:154` 並明言：
**身分可偽造＝用量歸屬可偽造＝部門月報表不可信**——計費正確的前提。

## 二、換成什麼（目標圖像）

**誰簽誰驗（08-01 問答補：簽章永遠只有平台在發，dev 只驗不簽也不領）**

| 誰 | 做什麼 | 用什麼 |
|---|---|---|
| CSP（平台） | 每次派工**簽**憑條 | 私鑰（只在平台手上） |
| dev 的 agent | 收到請求**驗**憑條 | 公鑰（自動抓 `/.well-known/jwks.json`，公開材料） |
| dev 本人 | 註冊時登記名字與 endpoint | **不領任何鑰匙**（對比今日：領 csk- 即背保管責任） |

驗簽材料（JWKS＋CA bundle）全是公開的，dev 不需要為驗簽申請任何東西；
憑條隨每次派工自動送達。唯一例外＝〈六〉待決題選 a 案時 agent 有一把**自己產**的鑰匙
（公鑰半邊登記給平台，仍非平台核發）、選 b 案才有一顆平台發的低權限小鑰匙。

- 平台**每次派工現簽一張 5 分鐘憑條**（`user_id, department, agent_id`），隨請求送 agent。
- agent 用平台公鑰驗簽（`/.well-known/jwks.json`，https＋CSPKI 信任錨）；改一字即失效、外洩有界。
- agent 任務內回呼平台（RAG 搜尋、trace、artifacts）**複用同一張憑條**；
  平台驗簽後照舊做 `bound_collection_id` 範圍檢查。
- agent 的 `.env` **不再保管任何長效祕密**（唯一例外見〈六、唯一待決〉的 b 案）。

## 三、設計原則（08-01 問答收斂）

1. **契約在 HTTP 層，不在框架層**：OpenAI 相容端點＋驗派工 JWT＋（選用）憑同一張 JWT 回呼。
   LangChain 等任何框架兩條路：用我們的 serving 殼裝它的腦（推薦），或完全自建、靠標準函式庫驗簽。
2. **接入成本三級制，氣隙內治理中心是唯一發行點**（內網無 PyPI）：
   ①新 agent 用範本＝零驗證碼，SDK **wheel 打包在範本 zip 內**（`pip install --no-index ./本地.whl` 離線可裝）；
   ②既有 Python 服務＝治理中心提供**單檔 `anila_verify.py`**，複製一檔＋import 兩行，零安裝零網路；
   ③零改碼＝**驗證 sidecar 容器**（驗完轉發，agent 本體不動），映像走內網既有搬運通道。
3. **相依上限＝stdlib＋cryptography**：否則單檔與 wheel 發行都退化成「再搬一串套件」。
4. **CA bundle**：範本內建、治理中心接入頁可下載（與驗簽片段並列）；
   SDK 明確吃 CA 檔設定，**不用 `SSL_CERT_FILE`**（取代整個信任庫的坑，平台側踩過）。
5. **誠實邊界**：驗證保護的是 agent 自己（防冒名直打 NodePort、保其日誌可信）；
   平台端計費歸屬不依賴 agent 驗不驗。制度責任＝把鎖門做便宜，不是逼人鎖門。

## 四、可沿用的既有資產

- RS256 keypair＋`/.well-known/jwks.json` **已上線**（`services/csp/app/api/jwks.py`）；
  Lab 端可達性 08-01 已實測（缺的只是 Lab 的信任錨，見 W0）。
- 每請求簽發短效 token 的現成範本：`services/csp/app/modules/launch/token.py`
  （10 分/14 claims 的 launch token；PLAN D2/B4 已決議轉用並收斂到規格的 5 分）。
- 兩份 production 級 JWKS 驗簽 client 可抄：anila-studio 與 asr-gateway 的 `jwks_client.py`
  （TTL 快取、unknown-kid 強制 refetch、背景刷新）。
- ⚠ agent 側（anila-core／anila-agent）**目前零 JWT 驗證程式碼**——W1 要新寫（抄上面）。

## 五、工作分解

### W0 環境水電（可先行，也是 e2e 驗收前提）
1. `cspki_ca_bundle.pem` 佈進 aiops Lab、驗證用 `curl --cacert`（000→403 即生效）。
2. FQDN 申請（擁有者送單，網管流程有等待期）；下來前 agent 端 `extra_hosts`（同 `.12` 前例）。
3. `.well-known` nginx 例外——本機已修（5898c49a），隨 P5.5 帶進 `.15`。

### W1 派工方向（CSP→agent）換簽章——SYSTEM-MAP 指定的核心
- CSP 派工時簽 5 分鐘 JWT（沿用 launch token 機制，claims 收斂到規格三件）。
- `build_agent_headers`（`services/csp/app/services/proxy/headers.py`）改造：
  拿掉 `X-CSP-Service-Token` 與明文 `X-ANILA-User-*`，身分全部進簽章。
- agent 側驗簽 middleware 新寫（fail-closed）；⚠ 一併殺掉舊 middleware 的 fail-open
  （`anila_core/api/middleware/auth.py:84`：空 token＝全放行）。

### W2 回程方向（agent→CSP）——csk- 的第二個角色，容易被漏掉
- 任務內回呼：複用派工 JWT；契約改動＝search API 從 `Bearer csk-` 改吃 `Bearer <JWT>`。
- Router／worker 的 `service_clients` 同一套 csk-——**同步換**，否則只拿掉一半。
- 任務外呼叫＝唯一待決，見〈六〉。

### W3 發行／管理面改造與交付
- **⚠ 前端 agent 頁面必做（擁有者 08-01 明確交辦，不得只改後端）**：
  治理中心 `DeveloperAgentsView.vue` 註冊精靈——Step 2「核發 service token」整段要拆掉
  （P2.1 之後註冊不發任何祕密）、`AgentGuardPanel` 的 csk- 接入片段換成 JWT 驗簽版、
  `.env` 產生器不再吐 `CSP_SERVICE_TOKEN`、加上「下載平台 CA」與三級接入指引。
  **驗收標準：走完註冊流程，畫面上不該再出現任何要使用者保管的字串。**
- 拆改：csk- 簽發、輪替、`bsk-` bootstrap、CLI `agent bootstrap`；
  `agent_credentials` AES 信封機制大幅縮水（或只留給 poll token）。
- 交付三級接入物：範本 zip（含 wheel＋CA）、單檔 `anila_verify.py`、驗證 sidecar 映像、
  治理中心「下載平台 CA」、JWT 版 guard snippets（取代現有 py/js/go/sh 四款 csk- 私規片段）。

### W4 文件／範本／測試全面換（機械量大、風險低）
- 全樹 **92 檔近 478 處**引用；大頭在文件（bootstrap protocol 是 frozen 文件要改版）、
  developer guide、治理中心文案、`.en.md` 孿生兩份、測試 fixtures。
- **「零 agent 時改契約免費」的實義**：沒有任何現場 `.env` 要遷移。

### W5 行為級驗收
- 偽造 `X-ANILA-User-*` → 拒收；篡改 JWT 任一 claim → 驗簽失敗；過期 → 拒收。
- JWKS 換鑰演練：`_serialize_jwks` 目前單鑰——順帶改多鑰，否則第一次換鑰全斷。
- 計費歸屬抽查（SYSTEM-MAP:154）。

## 六、Q19 裁決與分期(2026-08-01 擁有者拍板)

**裁決**:採「**非對稱 agent 身分 ＋ 平台推送 ＋ 短效任務授權**」。
③ 是架構方向,① 是身分底座,**不採 ②**(共享 `.env` 長效祕密)。全文見 OWNER-QUESTIONS Q19。

擁有者指出本文原本的論證漏洞:**③ 解決資料流,不解決身分。** 平台推送時仍須知道
推給誰、來的是不是本尊、是否已停用、是否送達、要不要重推。因此:

| | 證明什麼 | 效期 |
|---|---|---|
| **Agent 身分** | 我是哪一個 agent instance | 長期,agent 自產金鑰,平台只存公鑰 |
| **任務憑證** | 我現在被授權執行哪一項任務 | 5 分鐘,綁 `agent_id`＋`assignment_id`,限定 scope |

未派工只能:控制通道、heartbeat、收設定、收派工、回 ACK —— **不得存取業務資料**。
撤銷:短效 token 不需 agent 拉清單,**但長期身分必須有平台端停用／輪替／緊急斷線**,
緊急停止不能只等 5 分鐘過期。

### 分期(工程判斷,擁有者可推翻)

**第一期 = 今天已完成的**:任務憑證那一半。派工時簽 5 分鐘 JWT(claims 三件)、
agent 驗簽(JWKS,fail-closed)、任務內回呼複用同一張憑條、治理中心不再發祕密、
CA 與單檔驗證器可下載、文件全面改寫。**這一半自足**:agent 被派工時的身分已經是簽章的,
不再是明文標頭,計費歸屬可信 —— 也就是 SYSTEM-MAP:144-154 要的東西。

**第二期 = 身分底座**(尚未開工,**是一個子系統不是一包**):
公鑰註冊與輪替、控制通道、推送與 ACK、heartbeat、停用即時生效、緊急斷線。
規模明顯大於第一期。

**⚠ 時程實話**:8 月底上線是硬期限,而 **P5.5 整段未開始、`.15` 從未部署過任何一項**。
第二期若排在上線前,會與 P5.5 搶同一段時間。工程建議:
**上線前把第一期收乾淨(含把舊 csk- 的後端發行面拆掉),第二期排在上線後**,
理由是——第一期已經消除「使用者身分可被偽造」這個真正的風險;
未派工 agent 的身分目前仍靠既有的低權限服務憑證,那不是新風險,是既有現狀。
**若擁有者要求第二期進上線範圍,請直接說,我重排 P5 的順序。**

## 七、開工前要先確認的兩件（掃描標 UNVERIFIED）

1. 活體 DB `select count(*) from agents / agent_credentials`——「改契約免費」建立在真的是零上。
2. 422 修復（f7f91b86）後 `anila-core register` 的真實 round-trip 沒實測過——開工先跑一次。

## 八、順帶發現（已處理）

- `PLAN.md` 07-31 段原寫「X.8 已修但刻意未合併」已過時（wt/leak 已於 07-31 合併）——50ceec80 已修正。
