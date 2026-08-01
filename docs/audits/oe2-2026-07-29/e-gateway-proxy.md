> ⚠ **2026-08-01 P2.1**：agent 派工身分已改為平台現簽的 5 分鐘 JWT（JWKS 驗簽；
> 開發者不領 `csk-`／`CSP_SERVICE_TOKEN`）。下文保留當時紀錄，**勿依此做現行接入**；
> 現行上手見 `docs/guides/developer-guide.md` 與治理中心「接入驗簽 · 三級制」。

# OE-2 Spec-Conformance Audit — E. MODEL GATEWAY / PROXY

**Scope:** `services/csp/` — `app/api/proxy.py`, `app/services/proxy/{ceiling,headers,service}.py`,
`app/models/model_registry.py`, `app/api/models.py`, `app/schemas/model_registry.py`,
`app/services/auto_seed.py`, `app/api/router.py`, `tests/test_model_gateway_hardening.py`,
`migrations/versions/r1_0005_model_gateway_hardening.py`

**Spec authority:** `SYSTEM-MAP.md` (399 lines, repo root). All Q1 quotes below are verbatim from it
with line numbers. No category was assigned from code comments or from memory.

**Totals: CAT-A = 29 · CAT-B = 10 · CAT-C = 17**

---

## 0. Executive read

`r1_0005 "model gateway hardening"` added 7 columns to `model_registry`. Grepping every reader in the
tree: **5 of the 7 are never read by any code path** — they are written on create/update, echoed in the
GET response, rendered in the admin UI, and consulted by nothing. Those are the domain's core CAT-C mass.

The two genuinely load-bearing additions (`classification_ceiling`, `api_key_secret_ref`) survive:
the first by OE-1 disposition, the second as CAT-B.

The second CAT-C mass is **not** in r1_0005 at all — it is the Slice-2b-C/4a **task + span plumbing**
threaded through `proxy_request` / `proxy_stream` / `chat_completions` / `ceiling.py`. SYSTEM-MAP contains
**zero occurrences of 任務 or "task"** (verified: `grep -n "任務\|task\|Task\|工單\|狀態機"` → no hits),
and states the opposite requirement verbatim at L211.

The third is `auto_seed.py`, which is a **second, weaker registration path** that bypasses the SSRF guard
and contradicts two explicit spec rules (L94 不能自動核准, L197 UI-driven registration).

**Every security guard in this file set is CAT-A and none is recommended for converge.**

---

## 1. `model_registry` schema — r1_0005 + predecessors

| # | Construct | file:line | Doc ref | Q1 evidence (SYSTEM-MAP) | Q2 ongoing cost | Cat | Action |
|---|---|---|---|---|---|---|---|
| 1 | `protocol` col (`openai_compatible`/`custom_adapter`), NOT NULL | `app/models/model_registry.py:18-21`; `app/schemas/model_registry.py:20,43,70`; `migrations/versions/r1_0005_model_gateway_hardening.py:104-112`; `app/api/models.py:127` | doc 04 §2 | **No basis — and the spec text points the other way.** L199: 「通通走 OpenAI compatible;embedding 可能有 v2 變體(如 nv-embed-v2)」 — the spec asserts a single protocol, so a discriminator column has nothing to discriminate. (searched: protocol / 協定 / adapter / 適配 / OpenAI / compatible) | **Yes.** NOT NULL column + 3 pydantic fields + a mandatory `<select>` in the admin form (`apps/csp-governance-ui/src/views/ModelsView.vue:193-197`) that every model registration must answer + a table chip (`:53`). **Zero readers** — full-tree grep shows only write + echo. | **C** | **converge** — drop column, schema fields, UI select, chip; drop `test_model_gateway_hardening.py:148` assertion |
| 2 | `supports_streaming` / `supports_json_schema` / `supports_tools`, NOT NULL ×3 | `app/models/model_registry.py:39-41`; `app/schemas/model_registry.py:23-25,46-48,73-75`; `r1_0005:133-159`; `app/api/models.py:130-132` | doc 04 §2 | **No basis.** Only "tool" hit is L215 「像使用者透過 AI 開 tool」 — an analogy about *usage attribution*, not a capability declaration. (searched: 串流 / stream / tool / 工具 / json / schema / 結構化 / 能力 / capability) | **Yes, and actively misleading.** 3 NOT NULL columns + 9 pydantic fields. **Not editable in the admin form** (form exposes only protocol/ceiling/api_key), so every model displays the identical backfill chip set (`串流` only) — `ModelsView.vue:307-309,322-326`. **Zero readers**: nothing checks `supports_streaming` before streaming. | **C** | **converge** — drop all 3 columns, 9 schema fields, `CAPABILITY_LABELS` + `capabilityChips()` |
| 3 | `owner_department_id` FK → `departments` ON DELETE SET NULL | `app/models/model_registry.py:51-55`; `r1_0005:120-132,185-190`; `app/api/models.py:129` | doc 04 §2 | **No basis.** L202 makes model visibility explicitly assignment-driven, not department-driven: 「**每個人看到的模型清單不同**(依指派)」. L210 requires 部門 on the *usage row*, not on the model. §2's unit-admin scope (L69) is over people and usage, not model ownership. (searched: 部門 / 單位 / owner / 擁有 / 歸屬) | **Yes.** A live FK constraint into `departments` — every departments migration, delete path and re-parent operation in the P1.1–P1.4 tree work must account for it. **Zero readers.** | **C** | **converge** — drop column + FK; removes a constraint on the department-tree design |
| 4 | `api_key_secret_ref` per-model gateway key (`enc::v1::` envelope) | `app/models/model_registry.py:34`; `r1_0005:113-115`; `app/services/proxy/headers.py:205-235` | doc 04 §3 | **No direct basis.** L11/L33/L135 describe CSP *issuing* API keys to agents, not CSP *holding* upstream gateway keys. But L197-198 make endpoints plural and heterogeneous (「指向一個 endpoint」、「可能是 `https://<domain>/v1` 也可能是 `http://<host>:<port>`」), and >1 gateway makes one global `MODEL_GATEWAY_API_KEY` wrong — a derived, not verbatim, basis. | **Low.** Optional (NULL = global fallback), one password field in the form. | **B** | **keep + mark debt** — see debt notes D1, D2 |
| 5 | Write-only `api_key` in / `has_api_key` bool out; ciphertext & plaintext never returned | `app/schemas/model_registry.py:26-29,49-50,76-78`; `app/api/models.py:179-186,338-342,133` | doc 04 §3 | **Project security red line (secrets handling)** — CLAUDE.md §5 「祕密零外洩」. No SYSTEM-MAP text needed; per audit calibration this is non-negotiable. | n/a | **A** | **keep** — re-cite as red line, not doc 04 §3. `tests/…:122-136` locks it; keep that test |
| 6 | `classification_ceiling` col | `app/models/model_registry.py:37`; `r1_0005:116-119` | doc 04 §5 | **Pre-dispositioned KEEP (OE-1).** Supporting spec: L227 「密等在**專案啟動時就標好了**,平台主要是**記錄**它」; L241-242 the two lines. | n/a | **A** | **keep + re-cite §8**. ⚠ value set changes under OE-3 (5→4 levels); UI list `ModelsView.vue:303` still carries 極機密/絕對機密 |
| 7 | `health_status` five-state vocabulary + `health_checked_at` + in-place 3→5 value migration + `normalize_health_status` | `app/models/model_registry.py:28-29`; `r1_0005:78-101,160-169,172-181`; `app/services/health_checker.py:24-56`; `app/api/models.py:115-117` | doc 04 §9 / doc 01 §32 | **Capability yes, vocabulary no.** L290 「監控告警 \| **沒有** —— ISO 稽核已點名 \| **必做**」; L302 「`.12` 模型 gateway 呼叫連續失敗」. Neither the 5 names nor the old 3 appear anywhere. (searched: 健康 / health / 狀態 / degraded / 五態) | **Moderate.** Every reader must route through `normalize_health_status`; the DB carries legacy values needing normalization forever; the UI ships a `healthStatus` util. **Converging back now costs another migration for zero user-visible gain.** | **B** | **keep + mark debt** — do not extend; if the alerting rework (§9) touches it, collapse to healthy/unhealthy/unknown then. The **capability** (probe + alert) is CAT-A, row 22 |
| 8 | `is_internal` + `<owner-only>` / `<internal>` endpoint redaction | `app/models/model_registry.py:46`; `app/api/models.py:27-34,82-103` | migration 0033 (pre-doc-04) | **No basis.** L349 「agent/模型權限、跨使用者資料存取 \| A01 存取控制失效」 is about permission, not URL confidentiality. Not one of the enumerated red lines. (searched: 內網 / 白名單 / 存取控制 / 看得到 / 拓撲) | **Low.** One checkbox, two sentinels, a lock icon. Owner always sees the real URL. Predates doc 04 → Q3 fails. | **B** | **keep + mark debt** — see D3 (default-value mismatch), D8 (inconsistent with `/v1/agents`) |
| 9 | `model_type='agent'` rows in `model_registry` + `base_model_id` self-FK | `app/models/model_registry.py:13,58-59`; `app/services/auto_seed.py:271-314`; `app/services/proxy/service.py:183,194,208` | pre-doc | **Partial.** L216 「也要掛在 agent 上嗎? 要,兩邊都算 —— 報表要能回答「部門用了哪些 agent」」 gives a basis for agent-dimension attribution. The *duplication* with the `agents` table has none. | **Yes** — two registries to keep in sync — **but Q3 fails**: the field is genuinely read (`service.py:194,208` picks the agent header builder and withholds the gateway key from it), and it predates doc 04. | **B** | **keep + mark debt**; cross-domain — see D4 (dual agent registry, joint call with the agents lead) |
| 10 | `allowed_task_types` deliberately **not** added + test lock | `r1_0005:22-24`; `tests/…:96-97` | doc 04 §2 vs §11 | Correct outcome, moot rationale: SYSTEM-MAP has **no 任務/task concept at all** (0 grep hits), so no task-type column could be justified regardless of which doc section won. | Negligible (one negative assertion). | **B** | **keep + mark debt** — when converging the task plumbing (row 32), delete the test line and its arbitration comment together |

---

## 2. `app/api/models.py` — endpoint surface

| # | Construct | file:line | Doc ref | Q1 evidence (SYSTEM-MAP) | Q2 | Cat | Action |
|---|---|---|---|---|---|---|---|
| 11 | `_enforce_endpoint_url` — SSRF guard, `endpoint_kind="model"`, `ANILA_ALLOW_HTTP_ENDPOINT` split, trusted-hosts structured 400 | `app/api/models.py:38-79` | Slice 6a | **A — spec-cited AND red line.** L198: 「⚠ **端點可能是 `https://<domain>/v1` 也可能是 `http://<host>:<port>`。內網用 http 是常態,不是安全違規。** 現行程式碼把 http endpoint 當違規要改」 — the spec addresses this exact rule. Guard itself is a red line per calibration. | n/a | **A** | **keep + re-cite L198** (replace the doc ref). ⚠ see O1 — deploy-posture note |
| 12 | `GET /api/models` — list filtered by `user.allowed_models` for non-admin/owner | `:140-158` | — | L202 「**每個人看到的模型清單不同**(依指派)」 | n/a | **A** | keep + re-cite §6 L202 |
| 13 | `POST /api/models` — register (require_admin) | `:161-199` | — | L33 「治理:建知識庫、註冊 agent/模型」; L201 「誰能註冊:admin + 被 admin 授權的 dev」 | n/a | **A** | keep + re-cite §6 L201. ⚠ see O2 — gate is narrower than spec |
| 14 | `GET /{id}` with same visibility gate as list | `:298-311` | — | L202 (as above) + L349 A01 存取控制失效 | n/a | **A** | keep + re-cite |
| 15 | `PUT /{id}` (endpoint re-validated on change, `:327-328`) | `:314-358` | — | L33 治理; guard re-run is the red line | n/a | **A** | keep |
| 16 | `DELETE /{id}` deactivate + `POST /{id}/activate`; invariant `is_router_primary ⇒ is_active` | `:370-434` | — | L74 「停用/恢復自己單位的帳號」 establishes deactivate/reactivate as the platform's disable idiom; L117 主模型 backs the invariant | n/a | **A** | keep |
| 17 | `DELETE /{id}/purge` — owner-only hard delete, **409 when `token_usage` rows exist** | `:437-481` | — | L313 「**要留半年的稽核帳**、**要給長官的月報表** —— 全部在資料庫裡」; L275 稽核記 「刪除」 | n/a | **A** | **keep** — the 409 is what protects the 月報表 from a careless purge |
| 18 | `is_router_primary` + `GET /router-primary` (service token) + set/unset | `:202-295`; `app/models/model_registry.py:23`; migration `0008` | pre-doc | L117 「目前用指定的主模型;未來希望使用者可選自己被授權的模型」; L114 Router LLM | n/a | **A** | keep + re-cite §3 L114-117 |
| 19 | `log_audit_event` on create / update / deactivate / activate / purge / set- / unset-router-primary / health_check (×8) | `:190-198,258-266,283-294,349-357,389-398,424-433,472-480,497-507` | — | L275 verbatim: 「記什麼 \| 讀取受控文件/對話、上傳、刪除、改密等、匯出、列印、分享、登入登出、呼叫模型/agent、**管理動作**」 | n/a | **A** | keep + re-cite §8 L275 |
| 20 | `GET /{id}/health` — passive five-state read, permission-gated | `:515-543` | doc 04 §9 | L290/L302 (alerting is 必做) support a health *read*; the vocabulary is CAT-B row 7 | n/a | **A** | keep + re-cite §9 L290,302 |
| 21 | `POST /{id}/test` — active probe, persists status + latency, audits | `:546-561`, `_probe_and_persist :484-512` | doc 04 §9 | L302 「`.12` 模型 gateway 呼叫連續失敗」; L305 p95 門檻 backs latency measurement | n/a | **A** | keep — this is the surviving probe route |
| 22 | Background health loop → `upsert_alert` / `resolve_alert_by_fingerprint` | `app/services/health_checker.py:104-152` (followed, not audited) | doc 04 §9 | L290 「**必做**」 + L302 | n/a | **A** | keep — ⚠ see O3, the alert never fires on *real traffic* failures |
| 23 | `POST /{id}/health-check` — **deprecated alias** returning `deprecated: true` + pointer text | `:564-582` | Slice 6a | **No basis.** SYSTEM-MAP never mentions route deprecation or backwards compatibility; L322-326 §10 says `.15` is running the pre-redesign build and 「現有資料…**全部可刪** → 資料庫可以砍掉重來」 — there is no legacy client to preserve. | **Yes — a duplicated admin flow.** The only caller is **in this same repo**: `apps/csp-governance-ui/src/api/models.js:22` (`triggerHealthCheck`) alongside `:31` (`testModelConnection`). `ModelsView.vue` wires **both** as separate buttons (「健康探測」 `:493`, 「測試連線」 `:511`) that do the identical probe. Plus a permanent route, a test (`tests/…:204-218`) and a payload shape. | **C** | **converge** — delete the route, point `models.js:22` at `/test`, collapse the two UI buttons to one, delete `tests/…:204-218` |

---

## 3. `app/services/proxy/headers.py` — outbound header contracts

| # | Construct | file:line | Doc ref | Q1 evidence (SYSTEM-MAP) | Q2 | Cat | Action |
|---|---|---|---|---|---|---|---|
| 24 | `downstream_identity` — 員編 regex `\d{6,9}`, forwarded as plain-text `X-ANILA-User-Id`, omitted (never forged) for non-card accounts | `:117-135,173` | — | **A for the requirement, shape already scheduled.** L146 names it verbatim: 「**現況:可以被偽造。** CSP 送純文字標頭(`X-ANILA-User-Id`)加一把**全艦隊共用的靜態 token**」; L150-151 the fix: 「**正解:短效簽章 token。** CSP 派工時簽一個 5 分鐘有效的 JWT,內含 `{user_id, department, agent_id}`」 | n/a | **A** | keep the *identity propagation*; **known, scheduled as P2.1** for the shape. ⚠ see D5 — spec's claim set includes `department`, current headers do not |
| 25 | `build_model_gateway_headers` — 員編 **only**; structurally cannot carry `X-CSP-Service-Token`, email, groups, task or trace ids; destination-chosen (never caller-flag-chosen) at `service.py:194-205,517-528` | `:185-202`; `app/services/proxy/service.py:194-209,517-533` | doc 04 §3/AC5 | **Project security red line (credential scoping / 祕密零外洩).** No verbatim spec text, and per calibration none is needed. This is what prevents the `.12` gateway from ever holding a credential that impersonates CSP to agents, and prevents end-user PII entering model prompt logs. | n/a | **A** | **keep — never converge.** Re-cite as red line. The *structural* enforcement (builder has no such parameters) is the strong form; preserve it |
| 26 | `_apply_gateway_auth` — Bearer injection, model calls only, does not overwrite an existing `Authorization` | `:238-253` | Slice 6a | **Red line + environment fact** (CLAUDE.md §1: `.12` `/v1` requires Bearer). The model-only scoping is the same credential-scoping red line as row 25. | n/a | **A** | keep |
| 27 | `resolve_model_gateway_key` — per-model ref first, global env fallback | `:205-235` | doc 04 §3 | Rides row 4 (CAT-B). | Low. | **B** | keep + mark debt — see **D1** (silent fallback on decrypt failure) |
| 28 | `X-ANILA-User-Email` + `X-ANILA-User-Groups` on agent dispatch | `:141,174-177` | — | **No basis.** L151's JWT claim set is exactly 「`{user_id, department, agent_id}`」 — email and groups are not in it. L91 notes the card carries email but never says to forward it downstream. (searched: email / groups / 群組 / 個資 / PII) | **Yes.** These are PII crossing to a third-party MLSteam agent over what L148 itself calls a 「純 http NodePort」. **`user_groups` is entirely dead on the CSP side** — full-tree grep: no `app/` caller ever passes it (`service.py:195-201,518-524`, `api/proxy.py:909-916,1132-1134` pass at most identity+email); only tests do. Both must be decided again when P2.1 defines the JWT claim set. | **C** | **converge** — delete the `user_groups` parameter + header now (dead); drop `user_email` unless a named agent requires it, and if so carry it as a JWT claim in P2.1, not a plaintext header |
| 29 | Per-agent service-token 5-min TTL cache + `Lock` + `invalidate_agent_token_cache` rotation hook | `:20-73,92-109` | Sprint 8 X / Phase A decision #2 | **No basis.** L180 前綴快取 is model-inference caching, unrelated. (searched: 快取 / cache / 效能 / 輪替 / rotation) | **Yes — this is precisely a mechanism that needs a keeper.** Module-level mutable state + a lock + an invalidation hook every credential-write path must remember to call + a documented "stale token valid for the next 24h" grace semantics (`:26-29`). Saves one decrypt per outbound agent dispatch — noise against a multi-second LLM call. | **C** | **converge with P2.1** — signing a short-lived JWT removes the per-call DB decrypt that this cache exists to avoid, deleting the cache's entire reason to exist. Do not rip out standalone; fold into P2.1 |
| 30 | Legacy fleet-shared `CSP_SERVICE_TOKEN` fallback when `target_agent_id is None` | `:76-114` (esp. `:111-113`) | Sprint 8 X | **Spec names this as the vulnerability.** L146-148: 「加一把**全艦隊共用的靜態 token**。拿到那把 token 的人…就能冒充任何使用者」; L346 「對 agent 的身分靠純文字標頭 \| A07 身分驗證失效」 | **Yes.** An env var that grants any-agent impersonation. **Still reachable**: `service.py:195-201` calls `build_agent_headers(target_agent_id=target_agent_id)` where `target_agent_id` is `None` for `model_registry` rows of `model_type='agent'` — i.e. exactly the auto_seed-created rows of row 9. A cutover counter already exists (`app/api/usage.py:309-318`). | **C** | **converge with P2.1** — remove the fallback branch; the reachable path disappears when row 9's dual registry and row 42's env seeding are converged. Check the cutover counter reads zero first |

---

## 4. `app/services/proxy/ceiling.py`

`classification_ceiling`'s **existence** is confirmed KEEP (OE-1) and is not re-litigated. What follows
audits the machinery around it.

| # | Construct | file:line | Doc ref | Q1 evidence (SYSTEM-MAP) | Q2 | Cat | Action |
|---|---|---|---|---|---|---|---|
| 31 | Ceiling gate before outbound call — 403 + no upstream dispatch; `enforce_model_ceiling` / `enforce_agent_ceiling` | `:70-183`; wired at `app/api/proxy.py:829-835,1012-1018` | doc 04 §5/§8 | **Pre-dispositioned KEEP (OE-1).** Supporting: L227-228 the level set; L241 「可以做 = 密等 ≤ **營業秘密**」 | n/a | **A** | keep + re-cite §8. ⚠ thresholds change under OE-4, level set under OE-3 |
| 32 | PolicyDecision **deny** row on violation, with a human-readable `reason` | `:99-114` | doc 03 Done Criteria 4 | L275 「呼叫模型/agent」 is an audited action; L242 「要落稽核 = 密等 ≥ **營業秘密**」 — a denied call is by construction above a ceiling, hence classified. | n/a | **A** | keep + re-cite §8 L242,275 |
| 33 | PolicyDecision **allow** row recorded **only when task-linked** (legacy/task-less allows write nothing) | `:17-24,124-135`; test lock `tests/…:303-330` | this slice's own ruling | **No basis, and the predicate conflicts with the spec's.** SYSTEM-MAP's audit predicate is *classification level*, not task linkage: L238 table row 「**落稽核** \| **不用** \| **要** \| **要** \| **要**」 and L242 「要落稽核 = 密等 ≥ **營業秘密**」. 任務/task appears **0 times** in the spec. | **Yes — a live compliance gap.** A legacy (task-less) `/v1/chat/completions` at 機密 that passes the ceiling writes **no** decision row, while L238 says 密/機密 「要」落稽核. The gate keys on a concept the spec does not have, instead of the one it does. | **C** | **converge** — replace the predicate `task_ctx is not None` with `level >= 營業秘密` (exactly OE-4's line). Rewrite `tests/…:303-330`. ⚠ cross-domain: decide whether `policy_decisions` satisfies §8 稽核 or whether it must land in `audit_logs` |
| 34 | `TaskRunContext` import + `finalize_task_run(..., "failed")` **inside** the security gate | `:39,116-121` | Slice 2b-C | **No basis** (任務/task: 0 hits). L211: 「→ 一張表加幾個索引。**不需要 span 樹、parent 關係、trace id。**」 | **Yes.** The ceiling module — a security gate — imports the task-run lifecycle, so any change to task machinery ripples into the gate. | **C** | **converge** — make the gate a pure allow/deny + audit; move run finalization to the caller. Rides row 35 |
| 35 | `_effective_task_level` resolution order: task → conversation → 無機密 | `:44-67` | doc 04 §5 / doc 08 §4 | **Split.** *Conversation branch:* basis at L189 「⚠ **對話中途升密 → 之前萃取的記憶要撤回**」 — conversations demonstrably carry a level. *Task branch:* no basis. | Task branch rides row 34/38. | **A** (conv) / **C** (task) | keep the conversation branch + re-cite L189; drop the task branch with row 38. The 無機密 default is correct per L227 (「密等在**專案啟動時就標好了**」 — unlabelled genuinely is 無機密) |

---

## 5. `app/services/proxy/service.py`

| # | Construct | file:line | Doc ref | Q1 evidence (SYSTEM-MAP) | Q2 | Cat | Action |
|---|---|---|---|---|---|---|---|
| 36 | `_guard_outbound` — call-time SSRF re-validation (TOCTOU / DNS-rebinding), on all 4 outbound paths | `:180-185,506-511`; `app/api/proxy.py:901-903,1129-1131` | — | **Project security red line.** Registration-time validation alone is insufficient against DNS rebinding; this is the enforcing half. | n/a | **A** | **keep — never converge.** Verify any new outbound path adds it |
| 37 | `_emit_proxy_dispatch_spans` → `spans.record_proxy_dispatch`, emitted on success **and** failure, request **and** stream | `:88-118,445-454,456-469,759-778` | Slice 4a | **The spec forbids this in as many words.** L211: 「→ 一張表加幾個索引。**不需要 span 樹、parent 關係、trace id。**」 L393 lists under §13「這份圖譜沒有的東西」: 「agent 必須回傳 6 種 span 且單一根節點才准核准(trace-test)」. L399: 「需求是兩句話,做出來的是一套分散式追蹤治理協定。」 | **Yes.** Spans on every task-linked proxied call, ×4 call sites, plus a `spans` module, a traces table and a REST surface mounted at `app/api/router.py:70-73`. | **C** | **converge — strongest candidate in this domain.** Delete the emitter + its 4 call sites; hand the `spans`/`traces` module and the `traces_router` mount to their owning domain (row 51) |
| 38 | `task_id` / `task_trace_id` / `task_run_id` / `legacy_runtime_call` threaded through both public wrappers and both impls (14 signature slots) + dual usage-enqueue paths | `:133-138,311-344,387-470,489-491,661-692,695-778`; `app/api/proxy.py:809-816,866-869,987-994,1039-1042,1074-1077` | Slice 2b-C | **No basis** (任務/task: 0 hits). L210 fixes the required fields: 「要記的欄位:**時間、使用者、部門、模型、agent、token 數**。」 — no task, no run, no trace. L211 as above. | **Yes.** 4 extra params × 4 signatures; **two parallel usage-enqueue paths** (`enqueue_usage_task_linked` vs `enqueue_usage`) whose comments insist they stay byte-identical (`:308-310,658-660`); a `legacy_runtime_call` marker column on usage rows. | **C** | **converge** — drop the 4 params and collapse to one enqueue path. **Keep the usage row itself** (L210 is spec-required). ⚠ cross-domain: the `trace_id` column on `token_usage` is the usage domain's call, but L211 says it is not needed |
| 39 | Retry with exponential backoff on 5xx / timeout / connect-error (`PROXY_MAX_RETRIES`, `PROXY_RETRY_BASE_DELAY`) | `:211-233,348-384` | none (no doc citation) | **No basis, none needed.** L292 「預期問題 \| 太慢、答得不好、系統崩潰」 anticipates upstream failure; standard resilience, not doc-derived. | Mild and two-sided: retrying a 5xx three times with backoff on a 128K-context call (L179) compounds the 「太慢」 problem the spec names. | **B** | keep + mark debt — bound total wall-clock against `LLM_TIMEOUT`; see **O3** (retries do not feed the §9 alert) |
| 40 | Force `stream_options: {include_usage: true}` on every stream | `:534-536` | — | L210 「token 數」 must be recorded; L207 「**目的:算帳。長官月結看。**」 — real counts beat estimates. | n/a | **A** | keep + re-cite §7 L207,210 |
| 41 | Server-side token estimation when upstream omits `usage` | `:277-290,629-637` | Sprint 5 / Chunk W | L210 (token 數 must exist) — an estimate beats a zero in a billing table. | n/a | **A** | keep — but see **O4**: estimates are indistinguishable from reported counts in the billing table |
| 42 | `build_default_anila_meta` — permanently-empty contract fields `citations: []`, `confidence: None`, `handoff_chain: []`, `follow_ups: []`, and a synthesized `trace_id` | `:66-85` | pre-doc | **No basis for the four empties** (searched: 引用 / citation / 出處 / 信心 / confidence / handoff / 接手 / 追問 / follow-up → 0 hits). **`trace_id` is spec-negated** by L211. Basis exists for the neighbours: `trace[]` ← L116 「**使用者要看得到是哪個 agent 在回答**」; `latency_ms` ← L305 p95; `classified` ← §8; `usage` ← L37 「一般使用者的用量應該直接放進 ANILA」 | Marginal — constant values, but a **published wire contract** the ANILA frontend consumes. Removing risks frontend breakage for zero ops gain. | **B** | keep + mark debt — build nothing new on the four empties. ⚠ the synthesized `trace_id` (`f"trace-{int(time.time()*1000)}"`) is stored nowhere and collides across same-millisecond concurrent requests — do not treat it as an identifier |
| 43 | `requires_encryption` → one-way `classified` latch in `anila_meta` (never lowers; downstream `true` is authoritative) | `:59-64,299-302,552-571` | pre-doc | **A — the one-way upgrade latch is a hard spec requirement** (per audit calibration). Supporting: L189 「對話中途升密」 implies upgrade-only. | n/a | **A** | keep. ⚠ see **D6** — the *naming* violates L268 |
| 44 | base_url `/v1`,`/v2` double-version strip | `:162-171` | Sprint 5 / Chunk W | Defensive normalization that is exactly what makes L197-198's endpoint shape (「`https://<domain>/v1`」) work when concatenated with `/v1/embeddings`. | Negligible. | **B** | keep + mark debt (idempotent, tested by behaviour only) |

---

## 6. `app/api/proxy.py`

| # | Construct | file:line | Doc ref | Q1 evidence (SYSTEM-MAP) | Q2 | Cat | Action |
|---|---|---|---|---|---|---|---|
| 45 | `GET /v1/agents` — approved agents visible to the caller, incl. `description_for_router` | `:614-658` | pre-doc | L114 「**Router**:一個 LLM 讀使用者意圖,比對各 agent 的**自我描述**,決定派工或自己答」; L44 「選 agent / router 自動派」 | n/a | **A** | keep + re-cite §3 L114. ⚠ see **D8** — it returns `endpoint_url` to every permitted caller |
| 46 | `GET /v1/models` — OpenAI-compatible discovery, filtered by `check_model_permission` | `:661-699` | pre-doc | L199 「通通走 OpenAI compatible」; L202 「每個人看到的模型清單不同(依指派)」 | n/a | **A** | keep + re-cite §6 L199,202 |
| 47 | `POST /v1/chat/completions` — the spine (agent branch then model branch, streaming + non-streaming) | `:702-1087` | — | §3 L99-117 whole section; §4 L121-142 both directions | n/a | **A** | keep |
| 48 | `POST /v1/embeddings` + `POST /v2/embeddings` | `:1175-1218` | — | L199 「embedding 可能有 v2 變體(如 nv-embed-v2)」; L200 「**embedding 也算模型**,要能被指派與記帳」 — both routes and the permission+usage wiring are verbatim required | n/a | **A** | keep + re-cite §6 L199-200. ⚠ see **D7** — no ceiling check on these paths |
| 49 | `POST /v1/agents/{agent_name}/sessions/{session_id}/answer` — resume proxy for paused agent runs | `:1090-1172` | Sprint 13 PR A2 | **No basis.** §3/§4 describe a single dispatch→answer flow with no pause/interrupt step. (searched: 暫停 / 中斷 / 續問 / interrupt / session / 追問 / resume → 0 hits) | **Yes, and it has no caller in this tree.** A full extra route with its own passthrough streaming generator, its own SSRF guard call and its own SSE error framing. `apps/anila-shell/src/runtime/api.js:250` calls the **Router's** differently-shaped `/v1/sessions/{id}/answer`; `services/anila-core-router/main.py` (358 lines) implements no session/answer/interrupt route at all. | **C** | **converge** — delete unless the owner confirms a human-in-the-loop agent flow is in scope for August. If kept, it needs a caller and a §3 amendment |
| 50 | `_require_conversation_access` — caller-supplied `X-ANILA-Conversation-Id` must belong to the caller (admin-tier bypass) | `:51-65`, called `:733-734` | pre-doc | L166 「**對話** \| ANILA 與 ANILALM **各自獨立** \| 使用者自己管 \| **只有自己**(+ 分享)」; L349 「跨使用者資料存取 \| A01 存取控制失效」 | n/a | **A** | **keep** — this header drives memory writes and classification latching; without it one user mutates another's conversation metadata |
| 51 | Memory read-inject (`_inject_memory`) + post-turn write (`_schedule_memory_write`) + SSE tee (`_tee_stream_capture_assistant`) | `:199-249,467-495,498-573` | P3 / pre-doc | L184-187 「### 長期記憶(只有 ANILA)… **兩種都要**:使用者自己寫的偏好 + 系統自動萃取…萃取範圍**包含對話內容**(不只偏好)」 | n/a | **A** | keep + re-cite §5 L184-187. ⚠ see **D9** — L189's retraction requirement is unimplemented |
| 52 | Attachment injection `_inject_attachments` + `_sse_with_attachment_trace` (P1.5) | `:252-464,743-745,884,1057` | P1.5 (current work) | L173-177 verbatim flow: 「上傳 → 抽文字 → 算 token 數 ├─ 塞得下 128K(含對話歷史)→ 整份進 context ← 絕大多數 └─ 塞不下 → 才切塊檢索,並明白告訴使用者…」 | n/a | **A** | keep — current spec-driven work, not doc-derived. ⚠ see **D10** — the "told the user" half is partial |
| 53 | One-way classification latches `_latch_agent_classification` + `_latch_inherited_classification` | `:86-131`, called `:757,794-805` | Slice 3b / doc 08 §3-§4 | **A — one-way upgrade latch is a hard spec requirement** (calibration). Supporting: L189 「對話中途升密」; L188 「**密等內容不納入記憶**」 backs the memory-inheritance latch specifically | n/a | **A** | keep + re-cite §5 L188-189 (replace doc 08 refs) |
| 54 | `_propagate_conversation_level_to_task` | `:134-161`, called `:819-828,998-1007` | Slice 3b / doc 08 §4 | **No basis** — the target resource (task) does not exist in the spec (0 hits). | Rides rows 34/38. | **C** | **converge** with the task plumbing |
| 55 | `begin_task_run` in both chat branches + `finalize_task_run` in the non-streaming agent branch | `:809-816,952-953,956-972,987-994` | Slice 2b-C | **No basis** (0 hits); L211 negates the trace/parent model | Rides row 38. | **C** | **converge** with row 38 |
| 56 | `_agent_policy_level` — `requires_encryption=true` floors the level at 機密 | `:68-83` | doc 08 §4 | A boolean→level bridge; 機密 is inside the spec's set (L228 「無機密 / 營業秘密 / 密 / 機密」) but the bridge itself is a migration artifact. | Low. | **B** | keep + mark debt — **rides OE-3**; revalidate the floor when the level set collapses to 4 |

---

## 7. `app/services/auto_seed.py`

| # | Construct | file:line | Doc ref | Q1 evidence (SYSTEM-MAP) | Q2 | Cat | Action |
|---|---|---|---|---|---|---|---|
| 57 | Admin bootstrap: first run creates `ADMIN_USERNAME` with `role="owner"` | `:181-205` | pre-doc | L64 「**owner** \| 系統維運者 \| 全部」 — the owner tier must be reachable on a fresh deploy | n/a | **A** | keep |
| 58 | `AUTO_REGISTER_MODELS` (JSON) + `MODEL_<NAME>_<FIELD>` env-var model registration; endpoint re-synced on every boot | `:117-176,207-317` | pre-doc | **No basis, and it works against the spec's model.** L197 「**註冊方式**:指向一個 endpoint,把它 `/v1/models` 的模型**整批帶進來** —— 像 Open WebUI」 describes a UI-driven bulk import. L134/L142 are the only `.env` mentions and they are about MLSteam's collection IDs, explicitly 「不是在 CSP 綁定」. | **Yes — three distinct costs.** (a) **Bypasses the SSRF guard entirely**: `:253-263` constructs `ModelRegistry(...)` directly, never calling `models_api._enforce_endpoint_url`. (b) **Clobbers admin edits on restart**: `:265-267` overwrites `existing.endpoint_url` from env on every boot — the exact bug the Slice-7 rework fixed for links (see its own comment at `:496-503`) and never fixed for models. (c) `:155` hardcodes `http://{host}:{port}`, which the model endpoint-kind rejects at call time unless `ANILA_ALLOW_HTTP_ENDPOINT=1`. | **C** | **converge** — retire the env path in favour of the L197 bulk import; or, if kept for bootstrap, route it through `_enforce_endpoint_url` and make it create-only (never overwrite an existing row) |
| 59 | `AUTO_REGISTER_AGENTS` — `approval_status` defaults to `"approved"`; `approved_by = admin.id` written for any approved row | `:319-397`, esp. `:368-369,385-391` | pre-doc | **No basis; contradicts the audit-integrity requirement.** L63 「**admin** … + **核准帳號**」 makes approval a human act. L278 「防竄改 \| **要** —— 明確是為了防止 admin 權限的人偷偷做假」 — writing `approved_by = admin.id` when no admin approved anything is precisely a falsified approval record. | **Yes.** An env var mints an approved agent and attributes the approval to a real admin account. | **C** | **converge with OE-1's approval collapse** — seeded agents should land in `registered`; at minimum record the actor as a system-seed sentinel, never a human's id |
| 60 | `AUTO_SEED_API_KEYS` — creates users (`is_approved=True`, `is_active=True`), API keys and model/agent permissions from env | `:399-494`, esp. `:430-446` | pre-doc | **Contradicts the spec twice, verbatim.** L94 「⚠ **核准不是安全把關,是確保部門歸屬正確**(因為用量要按部門算),所以**不能自動核准**」 — the seed sets `is_approved=True`. L331 「**第一次刷卡才建帳號**(不預先匯入 3000 筆)」 — the seed pre-creates accounts. | **Yes.** Seeded users are approved without the act that establishes 部門歸屬, and land with `department_id` unset — L154 「身分能偽造,用量歸屬就能偽造,那份給長官的部門月報表就不可信」 applies to unattributed users too. | **C** | **converge** — restrict to non-production (`ANILA_ENV != production`) or drop. **Keep the fail-closed random-password discipline at `:422-429`** wherever this lands |
| 61 | `AUTO_REGISTER_LINKS` → `registered_services` with `config_source` / `env_seed_key` / `db_editable_fields` per-field ownership arbitration | `:37-114,496-509` | doc 07 §3/§15.1 | **Weak basis for the registry, none for the arbitration protocol.** §1 L19-33 establishes 三個網站 with ANILA as the landing page, so a links registry has some basis; the three-concept ownership protocol has none. (searched: 連結 / links / 服務 / 入口 / 註冊服務) | **Yes** — the operator must hold "which source owns which field" in their head to explain why an edit stuck. | **C** | **converge candidate — cross-domain.** Behaviour lives in my file, the model does not. **Hand the call to the services-registry / platform-links lead** |
| 62 | "empty `endpoint_url` = explicitly disabled" convention for optional compose services | `:226-240,323-335` | pre-doc | No basis (searched: 停用 / 選配 / optional). A compose-shaped convention. | Negligible. | **B** | keep + mark debt |

---

## 8. `app/api/router.py` — mount-level only

This file is pure wiring; the only auditable constructs are **which surfaces are mounted**. Rows below
cover the three mounts whose *surface* has no spec basis. The modules themselves belong to other domains
— **the converge call is theirs, not mine**; recorded here because the mount is in my file set.

| # | Mount | file:line | Q1 evidence | Cat | Action |
|---|---|---|---|---|---|
| 63 | `tasks_router` | `:37,68` | 任務/task: **0 hits** in SYSTEM-MAP | **C** (defer) | hand to tasks domain; rides rows 34/38/54/55 |
| 64 | `traces_router` | `:17,70-73` | L211 「**不需要 span 樹、parent 關係、trace id。**」; L393 trace-test listed under §13 | **C** (defer) | hand to traces domain; rides row 37 |
| 65 | `classification_inventory_router` | `:35,81-82` (comment cites doc 08 §15 "Classification Inventory Before Cutover") | **No basis.** L336-340 「### 上線前的安全要求(外部,非自訂)」 lists 源碼掃描 + 弱點測試 + OWASP + 紅隊 — no classification inventory. (searched: 盤點 / inventory / cutover / 上線前) | **C** (defer) | hand to classification domain |

Spec-backed mounts (no action): `auth`, `users`, `unit_admins`, `departments`, `usage`, `memory`,
`audit_logs`, `alerts`, `models`, `agents`, `api_keys`, `ingestion_*`, `jwks`, `trusted_hosts`,
`proxy`, `banners`, `platform_links`, `artifacts` (§11 Studio), `policy_decisions` (§8 稽核),
`service_*` (defer to services domain).

---

## 9. Tests — what the suite locks in

`tests/test_model_gateway_hardening.py` (401 lines). **Keep** the three security-discipline tests;
the rest pin spec-absent constructs and must move with them.

| Test | Line | Locks in | Fate |
|---|---|---|---|
| `test_build_response_never_leaks_secret` | `:122-136` | secrets never leave via the API (envelope prefix absent from the whole response) | **KEEP — red line** |
| `test_model_registration_allows_http_in_production_with_flag` / `…rejects_http_in_production_without_flag` / `…allows_http_in_dev` / `…https_ok_in_production` | `:225-247` | the owner-approved PLAN P0.2 flag split | **KEEP — red line**, aligns with L198 |
| `test_alembic_single_head_in_r1_namespace` | `:64-87` | one head, `r1_` namespace | **KEEP** — deliberately not head-pinned |
| `test_ceiling_deny_blocks_and_no_upstream_call` | `:278-300` | deny → 403, **zero** outbound calls (`respx.calls.call_count == 0`), deny row recorded | **KEEP** (OE-1) — reason string will change under OE-4 |
| `test_agent_ceiling_deny_blocks_before_dispatch` | `:362-383` | same for agents | **KEEP** (OE-1) |
| `test_r1_0005_revises_r1_0004` | `:90-97` | `allowed_task_types` absent from `upgrade()` — a doc-04 §2-vs-§11 arbitration with no spec meaning (row 10) | move with row 10 |
| `test_create_model_encrypts_write_only_api_key` | `:138-151` | asserts `resp["protocol"] == "openai_compatible"` (`:148`) — pins the **CAT-C `protocol` field** into the create/response contract | split: keep the key-encryption half, drop `:148` with row 1 |
| `test_normalize_health_status_maps_legacy` (8 params) + `test_normalize_disabled_when_inactive` + `test_get_health_maps_legacy_and_disabled` | `:156-186` | the **five-state vocabulary** incl. the legacy online/connecting/offline mapping (row 7, CAT-B) | keep while row 7 is CAT-B; delete if the vocabulary collapses |
| `test_legacy_health_check_alias_is_deprecated` | `:204-218` | the **deprecated alias route** and its `deprecated`/`detail` payload shape (row 23) | **delete with row 23** |
| `test_ceiling_allow_task_linked_records_allow` | `:303-319` | allow is recorded **because a task exists** — the spec-absent predicate (row 33) | **rewrite** to the L242 predicate |
| `test_ceiling_allow_legacy_records_no_decision_row` | `:321-330` | task-less allow writes **nothing** — this is the assertion that currently encodes the L238 compliance gap | **rewrite/invert** with row 33 |
| `test_agent_ceiling_allow_task_linked_records_allow` | `:386-401` | same predicate for agents | **rewrite** with row 33 |
| `test_ceiling_legacy_latched_conversation_deny` | `:343-359` | uses 「極機密」 (`:352`) — **a level outside SYSTEM-MAP §8's four** (L228 無機密/營業秘密/密/機密) | **will break under OE-3** — flag to that workstream |
| `test_resolve_uses_per_model_secret_ref` / `…falls_back_to_global_env` / `…none_when_no_key` | `:102-119` | the per-model→env fallback chain (row 4, CAT-B) | keep with row 4 |

**Files in my set with nothing auditable beyond what is tabled above:** none —
`app/api/router.py` contains only mounts (section 8) and `app/schemas/model_registry.py` contains only
the pydantic mirrors of section 1's columns (audited there, rows 1-8).

---

## 10. Debt notes (CAT-B carry-forward)

- **D1 — silent key downgrade.** `headers.py:226-235`: a *corrupted* `api_key_secret_ref` falls back to
  the global `MODEL_GATEWAY_API_KEY` with only a `logger.warning`. If the two keys address different
  gateways, the call silently authenticates against the wrong one. Fail-soft on the key *source* is
  defensible; make it observable (alert, or a `has_api_key`-vs-`key_usable` distinction in the UI).
- **D2 — no way to clear a per-model key.** `models.py:340-342` only overwrites on non-empty input;
  there is no path to null `api_key_secret_ref` back to the global fallback. Rotation works, un-setting
  does not.
- **D3 — `is_internal` default mismatch.** `ModelCreate.is_internal = True` (`schemas:18`) vs DB
  `server_default False` (migration 0033). An admin who does not untick registers an external LAN box
  that other admins see as `<internal>`. Cosmetic only (redaction is by role, not by this flag).
- **D4 — dual agent registry.** Agents exist both as `agents` rows (what `_resolve_agent` uses) and as
  `model_registry` rows with `model_type='agent'` (what `auto_seed:271-314` writes). This is also the
  only remaining path that reaches the legacy fleet token (row 30). Joint call with the agents lead.
- **D5 — P2.1 claim set.** L151 specifies `{user_id, department, agent_id}`; today's headers carry
  員編 + email (+ a dead groups slot) and **no department**. Since L154 ties 部門 to billing
  trustworthiness, `department` should be in the JWT.
- **D6 — honesty of naming (spec-cited).** L268: 「**UI 上的字必須誠實** —— 不可以寫「已加密」,要寫
  「列管」或「受控存取」」, and L347 lists 「「加密模式」宣稱加密但沒加密 \| A02 加密失效」. The wire
  field `classified` is fine; the underlying flag is `requires_encryption` and it is surfaced by name
  in `api/proxy.py:654` (`/v1/agents` response). Rename toward 列管/受控存取 when OE-3 touches the levels.
- **D7 — ceiling asymmetry.** `enforce_model_ceiling` guards `/v1/chat/completions` (`proxy.py:1012`)
  but **not** `/v1/embeddings` or `/v2/embeddings` (`:1175-1218`). Classified text reaching an embedding
  model is an outbound call too. §8's table does not enumerate embedding, so this is a judgement call —
  raise it when OE-4 rewrites the thresholds.
- **D8 — `/v1/agents` leaks endpoint URLs.** `proxy.py:651` returns `endpoint_url` to every permitted
  caller, including user-tier. This is the inverse of the owner-only redaction that `models.py:82-103`
  applies to model endpoints, and L148 says knowing the endpoint (「能直接打 agent endpoint 的人
  (純 http NodePort)」) is half of the A07 impersonation path. Redact for non-admin.
- **D9 — memory retraction unimplemented (spec gap).** L189: 「⚠ **對話中途升密 → 之前萃取的記憶要撤回**
  → 每則記憶要記得來源對話,才找得到」. `_latch_agent_classification` / `_latch_inherited_classification`
  (`proxy.py:86-131`) raise the conversation level but nothing retracts memories previously extracted
  from that conversation. Cross-domain (memory), surfaced from the latch call sites.
- **D10 — attachment overflow message is partial.** L176: 「塞不下 → 才切塊檢索,並明白告訴使用者
  「這份太大,我用檢索的,可能會漏」」. `_inject_attachments` reports 「未納入 N 份」 (`proxy.py:368-372`)
  — omitted, not retrieved — and there is no chunk-retrieval fallback on this path. Likely already in
  the P1.5 plan; recorded for completeness.

---

## 11. Adjacent observations (not findings; no category)

- **O1 — deploy posture inversion.** L198 says 「**內網用 http 是常態,不是安全違規**」, yet the default
  posture still rejects http and requires `ANILA_ALLOW_HTTP_ENDPOINT=1` (owner-approved PLAN P0.2 flag
  split — correct as recorded). Consequence: on the `.15` intranet deploy the flag is effectively always
  on, so the "default" posture is the never-used one. Ensure the deploy checklist sets it, or the first
  intranet model registration fails with a 400 that looks like a bug.
- **O2 — registration gate narrower than spec.** L201: 「誰能註冊:admin + 被 admin 授權的 dev」.
  `POST /api/models` uses `require_admin`, which is `{admin, owner}` only (`auth_service.py:145-157`);
  `developer` cannot register a model. A gap, not over-engineering — flagged so it is not mistaken for
  one during the converge.
- **O3 — the §9 gateway alert never fires on real traffic.** L302 requires an alert on
  「`.12` 模型 gateway 呼叫連續失敗」. Alerts are raised **only** by the background health loop
  (`health_checker.py:123,175`); `proxy/service.py` imports no alerting at all, so a gateway that 502s
  every real user request while answering `/health` raises nothing. Verified by grep across
  `app/services/proxy/` and `app/api/proxy.py`.
- **O4 — estimated tokens are indistinguishable from reported ones.** `service.py:277-290,629-637`
  writes server-side estimates into the same `token_usage` fields as upstream-reported counts, marked
  only by a `logger.warning`. L207 「目的:算帳。長官月結看。」 — a 月報表 cannot tell measured from
  estimated. Usage domain's call; surfaced from the proxy write site.
- **O5 — spec-required bulk import does not exist.** L197: 「**註冊方式**:指向一個 endpoint,把它
  `/v1/models` 的模型**整批帶進來** —— 像 Open WebUI」. There is no such endpoint anywhere in `app/`
  (grep). The current shape is the opposite: one form, one row, with `protocol` / ceiling / capability
  fields to fill per model. Converging rows 1-3 removes friction on the path to the spec's shape.
- **O6 — auto_seed swallows all failures.** `:316-317,396-397,493-494,512-514` catch bare `Exception`,
  log, and let boot proceed with a partially-seeded registry. Against L289 「系統要能自己喊救命」 this
  is the wrong default, though it is not a doc-derived construct.
- **O7 — non-streaming agent forwards write no usage row.** Acknowledged in-code at
  `proxy.py:905-908`. Under L210/L216 (「兩邊都算」) that is a billing hole, pre-existing and orthogonal
  to this audit.

---

## 12. Recommended converge order

1. **Rows 1, 2, 3** — drop `protocol`, `supports_*`, `owner_department_id` in one migration + one UI
   commit. Zero readers, zero behaviour change, removes an FK constraint from the department tree.
2. **Row 33** — swap the PolicyDecision allow predicate to L242's line. Do this **inside OE-4**, since
   OE-4 is already rewriting that threshold. Closes a live §8 compliance gap.
3. **Rows 37, 38, 34, 54, 55** (+ mounts 63, 64) — the task/span plumbing, as one change set. Largest
   removal; spec-negated verbatim at L211.
4. **Rows 58, 59, 60** — `auto_seed` model/agent/user paths. Closes an SSRF-guard bypass and two direct
   spec contradictions.
5. **Rows 23, 49** — deprecated health alias and the orphaned resume proxy. Small, self-contained.
6. **Rows 28, 29, 30** — fold into **P2.1**, not before: the JWT rewrite deletes their reason to exist.
