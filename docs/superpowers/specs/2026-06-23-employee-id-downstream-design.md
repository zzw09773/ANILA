# 設計：送訊息時下傳員編（員工編號）

- **日期**：2026-06-23
- **狀態**：設計 v2（已納入 Codex 審查；尚未實作、尚未 commit）
- **分支範圍**：**只落 `prod-intranet-card`**（不從 `main` 起、不散其他 6 分支）
- **撰寫**：Claude → Codex 審（v1 已審，見 §8）→ Claude 實作 → Codex 審實作

---

## 1. 背景與問題

內網同事反映兩個需求：

1. **追溯源頭**：送訊息時帶上員編，作為稽核追溯依據。
2. **agent 需要員編做事**：本院系統基本上以員編帶出系統內資訊，agent 對院內系統 request 時需要員編才能查到該員資料。

現況（`prod-intranet-card`）：CSP 轉發給下游時送的 `X-ANILA-User-Id` 值是 **`user.id`（資料庫自增主鍵 PK）**，不是員編。對上述兩個需求都無用。

員編來源：卡片登入時 `card_auth` 從 X.509 `subject.serialNumber` 抽出員編（例 `990000001`），並以 `username = employee_id` 建立/比對帳號。因此**本分支 `user.username ≡ 員編`**（卡登為唯一登入方式；唯一例外是 admin 帳密登入，其 `username = "admin"`）。

## 2. 已定案決策

| # | 決策 | 選擇 |
|---|---|---|
| D1 | 員編下傳範圍 | **全鏈含模型**（agent + 模型 .12 都送） |
| D2 | 員編的信任性質 | **授權鑰匙**：院內系統信任 agent 斷言的員編去撈該員資料 |
| D3 | header 設計 | **B-fixed**：沿用單一 `X-ANILA-User-Id`，值改員編；連同把記憶端點改成「以員編 resolve」 |
| D4 | 旗標 | **不要旗標**，員編無條件送 |
| D5 | 分支範圍 | **只落 `prod-intranet-card`** |
| D6 | 明文 LAN | **接受**（員編為 pseudonymous 穩定識別子；MLSteam agent 為純 http NodePort） |
| D7 | email/groups 進模型 | **否**，只員編進模型 |
| **D8** | **confused-deputy 記憶端點** | **整個刪除**（架構釐清:facts 給 Router 個人化、CSP 已 push→pull 端點多餘;零消費者確認後刪端點 + anila-core 死 client→漏洞消失、零能力損失。見 §4.5） |

## 3. 設計

### §1 Wire 契約（單一 header，值=員編）

| Header | 改動前 | 改動後 |
|---|---|---|
| `X-ANILA-User-Id` | `str(user.id)`（PK） | 員編（`user.username` 經 `^\d{6,9}$` 驗證後） |
| `X-ANILA-User-Email` / `-Groups` | agent 路徑才送 | 不變（仍只 agent 路徑） |
| `X-CSP-Service-Token` | agent 路徑送 | **僅 agent 路徑**；**模型閘道路徑絕不送**（見 §2 CRITICAL） |

- **agent 路徑**：完整身分（員編 + email + groups + service token），恆送。
- **模型路徑（.12 / 嵌入）**：**只**送 `X-ANILA-User-Id`=員編 + `Authorization: Bearer MODEL_GATEWAY_API_KEY`。不送 email/groups，**不送 service token**。
- **身分驗證 fail-closed**（取代 v1「admin 送 `"admin"`」）：下游身分值必須符 `^\d{6,9}$`。非卡片帳號（admin / username 非員編）**不送身分 header**（fail-closed），不送 `"admin"`，避免污染「員編」契約。

### §2 兩個 header builder（CRITICAL：拆開 service token 與身分）

> Codex v1 [CRITICAL]：現況 `_build_downstream_headers()` 把 `X-CSP-Service-Token` 與 `X-ANILA-User-Id` 綁在同一個 builder，且 `target_agent_id=None` 時還會 fallback 到 legacy `CSP_SERVICE_TOKEN`（`proxy_service.py:121,138`）。若為了讓模型收員編而把 direct-LLM path 改成 `inject_identity=True`，**模型閘道會拿到 CSP service token**。

拆成兩個函式：

- `build_agent_headers(user_identity, user_email, user_groups, target_agent_id)` → 完整身分 + service token（即現 `_build_downstream_headers` 的角色）。
- `build_model_gateway_headers(user_identity)` → **只**有 `Content-Type` + `X-ANILA-User-Id`=員編 + `Authorization: Bearer`（由 `_apply_gateway_auth`）。**永不**含 `X-CSP-Service-Token`。

Builder 由**目的地**決定（`proxy_request`：`model.model_type == "agent"`；`proxy_stream`：`target_agent_id is not None`），**不由呼叫端旗標**——`inject_identity` 參數已**移除**（impl 審查 [CRITICAL]：留著旗標＝誤設就外洩 token）。誤設也無法把 service token 送上模型閘道＝結構性保證。

### §3 FK-vs-header 拆參數（避免汙染 usage FK）

> Codex v1 [HIGH] 確認：`token_usage.user_id` 是 `ForeignKey("users.id")`（`token_usage.py:11`），而 `proxy_request/proxy_stream` 用同一個 `user_id` 既建 header 又寫 `enqueue_usage`（`proxy_service.py:499,608,688,825`）。

`proxy_request` / `proxy_stream` / 兩個 builder 的簽名拆成：

- `user_id: int`（PK）—— **只**餵 `enqueue_usage`（維持 `token_usage.user_id` 永為 `users.id`）。
- `user_identity: str`（員編）—— **只**給 wire header。

`proxy.py` **每個** chat call site 明確同時帶 `user_id=user.id, user_identity=<驗證後員編或 None>`，含直呼 `build_agent_headers` 的 567 / 711（resume-answer）。新增測試斷言 usage 一定寫 PK、header 一定寫員編，兩者不交叉。

### §4 記憶契約改「resolve 不改鍵」+ 信任硬化

**§4.1 resolve 不改鍵**（Codex v1 [HIGH] 確認方向對、且確認三個改點）
CSP `user_facts` / `conversation_memory_chunks` 維持以 `user_id = users.id`（PK）為外鍵儲存；只改 agent 入口端點查法：

- `app/api/memory.py::list_user_facts_for_agent`（`memory.py:261,263,285`）：path `user_id: int` → `user_identity: str`；`User.username == user_identity` resolve→ 取 `user.id` → `UserFact.user_id == user.id` 查；未知/admin 走既有 404。**儲存零搬遷。**
- `anila-core/.../caller_context.py`（`:49,:93-98`）：`CallerContext.user_id` `Optional[int]` → stripped `str | None`，移除 `int()`；docstring 更新。
- `anila-core/.../http_user_facts.py`（`:79-80`）：`get_user_facts(user_identity: str)`，URL 帶員編。
- `factory.py`（`:47-56`）：`has_callback_credentials` 須擋空白字串（`""` 不得通過）。

**§4.2 agent 端 fail-closed（硬性前提）**
> Codex v1 [HIGH] 確認：`anila-core/.../middleware/auth.py:84` `if self._dev_mode or not self._service_token: return await call_next(request)` —— **未設 token 即放行全部**（fail-open）。`RotatingServiceTokenMiddleware` 同有「無 token → local dev 放行」路徑。

員編=授權鑰匙的前提是 agent 真的驗 service token。**prod-intranet-card 部署硬性要求**：agent 必須走 fail-closed（MLSteam 用的 `anila-agent/serving/auth.py` 是 fail-closed，`allow_unset=False`），且**確保 `CSP_SERVICE_TOKEN`/per-agent token 有設、`dev_mode=False`**。部署驗證須涵蓋「無 token 時 agent 回 401 而非放行」。

**§4.3 員編只由 CSP 斷言**
值取自 `caller.user.username`（伺服端、卡登驗章後），**絕不回顯客戶端送進來的 header**。

**§4.4 服務憑證不外流 + 明文 LAN**
service token **只走 agent 路徑、絕不上模型閘道**（§2）。員編走純 http LAN（D6 接受）；更正用語：員編是 **pseudonymous 穩定識別子**（非 de-identified），可經 mapping 對回個人。緩解：LAN network ACL、agent token 短 TTL/輪換；可行時上 TLS/mTLS。

**§4.5 confused-deputy（D8）→ 已移除（刪除端點 + 死 client）**
> Codex v1 [HIGH]：`list_user_facts_for_agent` 只認「是 agent token」就放行讀任意 user facts，無 agent↔user 綁定。被盜 agent 可枚舉員編撈全院記憶。

**決議（D8，user 拍板）：整個刪除。** 架構釐清——UserFacts 是給 **ANILA Router 個人化**用的（Router 拿 agent 產出 + 使用者記憶**重組**最終回覆，像照 CLAUDE.md 重組語言），**不是給葉子 agent**。而 CSP 早已在 `proxy.py:_inject_memory` 把使用者記憶 **push** 進（以 agent 身分被呼叫的）Router 的 prompt（伺服端用 `user.id` 取、agent 偽造不了）→ 這個 **pull** 端點對此架構**多餘**。實查確認**零消費者**（MLSteam anila-agent 用 memdir + CspHttpRetriever，不接此端點）後，**整個移除**：CSP `list_user_facts_for_agent` 端點 + anila-core `HttpUserFactReader`/`factory`/`make_user_memory_reader` 死 client + `long_term` exports + 相關測試。**漏洞消失、零能力損失**——不為架構上不需要的端點去造安全機關。

（衍生新功能「Router 用使用者記憶重組回覆」= `user>router>agent>router>user`，是獨立的 anila-core Router 工作：`router_server.py` 目前對 dispatched 回覆 verbatim 轉出，需改成重組 + 加系統提示，另案。）

### §5 受影響檔案（精確清單）

**anila-core（本分支 delta）**
- `api/caller_context.py` — `user_id` int→`str|None`、移除 `int()`、docstring。
- `memory/long_term/clients/http_user_facts.py` — `get_user_facts(user_identity:str)`、URL 值。
- `memory/long_term/clients/factory.py` — `has_callback_credentials` 擋空白字串。

**CSP backend（本分支 delta）**
- `app/services/proxy_service.py` — 拆 `build_agent_headers` / `build_model_gateway_headers`（§2）；`proxy_request`/`proxy_stream` 新增 `user_identity`、模型路徑改走 model builder（**非** `inject_identity=True`）；`enqueue_usage` 續用 `user_id` PK。
- `app/api/proxy.py` — 每個 chat call site 補 `user_identity=<驗證後員編>`（agent stream ~:507、non-stream agent ~:555、resume ~:711；direct-LLM stream/non-stream ~:613/:631 改走 model builder 以送員編）。身分 `^\d{6,9}$` 驗證、非卡片帳號不送。
- `app/api/memory.py` — `list_user_facts_for_agent` 以 `user_identity` resolve（§4.1）。(+ D8 若選收斂：加 agent↔user 綁定。)

**anila-agent**
- 功能無改（`service_wrapper` 已把 `X-ANILA-User-Id` 當 opaque 字串 tenant、`AuditHooks` 照記）。
- `memory/extract.py`（`:22-31`）[LOW]：去敏時把**當前 identity 的 exact value** 一併 redact；**不要**用泛用 `\d{6,9}` regex（會誤傷一般數字）。

### §6 測試

- 改既有釘死 numeric `42` 的測試：`anila-core/tests/test_caller_context.py:26-101`、`anila-core/tests/test_memory_user_http_client.py:19-138`。
- `caller_context`：員編 `"990000002"`、`admin`、空白 header（→ user_id None）三案。
- CSP `list_user_facts_for_agent`：員編 resolve→facts、admin/未知→404。
- header builder：`build_model_gateway_headers` **不含** `X-CSP-Service-Token`（CRITICAL 回歸鎖）；`build_agent_headers` 含完整身分；模型路徑 header 無 email/groups。
- 拆參數：usage 永遠寫 `users.id`(PK)、header 永遠寫員編；非卡片帳號不送身分。
- 路徑覆蓋：agent stream/non-stream、LLM gateway stream/non-stream、resume、embeddings（不注入身分）。
- 跑全後端 + anila-core + anila-agent，**分清 regression vs pre-existing**。

### §7 落地與分支

- **只落 `prod-intranet-card`**，直接 commit，不從 main 起、不 cherry-pick。
- 共用檔（anila-core / proxy_service / memory.py）在 card 分支形成「員編身分 delta」，等同登入/部署 delta 的**受保護區**；日後 `main → prod-intranet-card` 同步須保留、勿覆蓋（`AGENTS.md` §3）。
- 每個部署 agent 端（MLSteam）記憶分艙 key 由 PK→員編，一次性重分；**CSP 端記憶資料不動**。
- **D8 已決：刪除**——confused-deputy 記憶端點 + anila-core 死 client 整個移除（CSP push 已涵蓋 Router 個人化需求,pull 多餘）；衍生「Router 重組」新功能另案。

## 4. Non-goals

- 不改 CSP `user_facts` / 記憶儲存 schema（無資料遷移）。
- 不上其他 6 分支、不從 main 起、不做環境旗標。
- 不為本案上 TLS（明文 LAN 已接受；改以 network ACL + token 輪換緩解）。
- agent → 院內系統的串接本身（agent / 院內系統側，非本 repo）。

## 5. 風險與緩解

| 風險 | 緩解 |
|---|---|
| 模型閘道誤收 CSP service token | §2 拆 builder，model builder 永不含 service token；測試回歸鎖 |
| 員編洩進 usage FK / user.id 仍當 wire 身分 | §3 拆 `user_id:int` / `user_identity:str`；測試斷言不交叉 |
| agent fail-open（未設 token） | §4.2 部署硬性 fail-closed + 驗「無 token 回 401」 |
| confused-deputy 跨租戶讀記憶 | §4.5 / D8（收斂或文件化） |
| 共用檔 card 分支 delta 被 main 同步覆蓋 | 列受保護區、挑選式保留 |
| admin/非員編 身分污染契約 | §1 `^\d{6,9}$` 驗證、非卡片帳號 fail-closed 不送 |
| 員編 pseudonymous 走明文 LAN | network ACL、token 短 TTL/輪換、可行時 TLS |

## 6. 驗證計畫（實作後）

- `myCSPPlatform/backend/.venv/bin/python -m pytest` + anila-core/anila-agent 測試綠。
- 端到端：卡登 → 對 agent 送訊息 → 確認 agent 收到 `X-ANILA-User-Id`=員編、**無** service token 洩漏到模型；對模型送訊息確認只帶員編、無 email/groups、無 service token。
- `/api/memory/users/{員編}/facts` 員編可取回、未知 404；無 service token 時 agent 回 401。
- 交 Codex 審實作。

## 7. （此節併入 §3-§7，保留編號占位）

## 8. Codex 審查納入紀錄（v1，2026-06-23）

| 來源 | 等級 | 處置 |
|---|---|---|
| service token 與身分綁同 builder，恐洩到模型閘道 | CRITICAL | 納入 §2（拆兩 builder） |
| `user_id` 同餵 usage FK 與 header | HIGH | 納入 §3 |
| proxy.py 多個 bypass call site / resume / direct-LLM 現未送身分 | HIGH | 納入 §5 |
| 記憶端點/caller_context 吃不下非數字員編 | HIGH | 納入 §4.1 |
| confused-deputy：任何 agent token 讀任意 user facts | HIGH | §4.5 / **D8 待決** |
| anila-core middleware 空 token fail-open；員編非 de-identified | HIGH | 納入 §4.2 / §4.4（更正用語） |
| admin 邊界不能只靠不 crash | MEDIUM | 納入 §1（`^\d{6,9}$` fail-closed） |
| 測試/型別 ripple（含 test_memory_user_http_client、空白字串 guard） | MEDIUM | 納入 §6 / §4.1 |
| extract.py 不 redact bare 員編、勿用泛 regex | LOW | 納入 §5（redact exact value） |

**Codex v1 VERDICT**：不建議照 v1 實作；須先（1）拆 identity/header builder、（2）完整拆 `user_id:int`/`user_identity:str`、（3）記憶/caller_context 改 username-resolve-to-PK，並補「模型閘道不拿 service token」「prod agent fail-closed」。→ 以上均已納入本 v2，餘 **D8** 待 user 拍板。

## 8.1 Codex 實作審查納入紀錄（impl v1，2026-06-23）

實作完成後再交 Codex 審 diff，抓到並已修：

| 來源 | 等級 | 處置（已實作 + 測試鎖） |
|---|---|---|
| builder 仍由 `inject_identity` 旗標決定（誤設即外洩 token 到模型閘道） | CRITICAL | builder 改**目的地驅動**、`inject_identity` 參數**移除**；加 `proxy_stream` 路由整合測試（設 legacy token 仍不外送） |
| `list_user_facts_for_agent` 未限員編格式即 resolve username（`/users/admin/facts` 可讀 admin facts） | HIGH | 查 DB 前先驗 `^\d{6,9}$`，不符→404（fail-closed）；加純單元測試 |
| `has_callback_credentials` 對 service_token/csp_base_url 仍 `is not None`（空字串可過） | MEDIUM | 三欄改 stripped truthiness；加空白 token/base URL 測試 |
| 測試只鎖 builder、未鎖 proxy 路由不變量 | MEDIUM | 加 `proxy_stream` 模型路由「不送 token」整合測試 |
| `user_groups` 未串進 proxy（agent path 不送 groups） | LOW | **pre-existing**（原 `_build_downstream_headers` 的 chat 呼叫端本就未傳 groups），非本案回歸；維持現狀 |

**測試結論**：員編相關測試全綠（CSP `test_employee_id_downstream` + `test_proxy_stream_usage`/`test_ssrf`、anila-core `test_caller_context`/`test_memory_user_http_client`）。完整後端套件 **43 failed / 12 error 為 pre-existing**（SQLite 跑不動 PG `JSONB` → alembic upgrade 失敗 + 測試隔離污染；stash 我的改動後 baseline 同數）→ 本案**零新增失敗、僅多通過**。

---

## 附錄：關鍵程式位置（設計當下）

- `myCSPPlatform/backend/app/services/proxy_service.py:121/138`(builder)、`:499/608/688/825`(usage)、`:691-700`(stream model auth)
- `myCSPPlatform/backend/app/api/proxy.py:~507/555/613/631/711`(call sites)
- `myCSPPlatform/backend/app/api/memory.py:261/263/285`(list_user_facts_for_agent, PK)
- `myCSPPlatform/backend/app/models/token_usage.py:11`(user_id FK)
- `anila-core/src/anila_core/api/caller_context.py:49/93-98`
- `anila-core/.../clients/http_user_facts.py:79-80`、`factory.py:47-56`
- `anila-core/src/anila_core/api/middleware/auth.py:84`(fail-open)
- `anila-agent/anila_agent/serving/auth.py:18-42`(fail-closed 對照)、`memory/extract.py:22-31`
