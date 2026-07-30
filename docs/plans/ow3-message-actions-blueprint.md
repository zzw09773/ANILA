# OW-3 Implementation Blueprint — Message-level Custom Action Buttons

> Authored by feature-dev:code-architect (opus), 2026-07-30. Saved verbatim by commander.
> Repo `/home/c1147259/桌面/ANILA/anila-restart-20260729/ANILA`, branch `restart/from-redesign`, HEAD `fb63005`. Alembic head `r1_0012` → new `r1_0013`.
> Spec basis: `PLAN.md:130-147` (OW-3 + 三件連帶 + 機制 table), `SYSTEM-MAP.md:225-281` (§8). Depends on OW-1 (landed).
> Commander note: backend pytest baseline at this HEAD = **797 passed / 37 failed / 1 skipped / 2 errors** (fail set = the known env-dependent block, stable across OE-3/OE-4/OW-1).

## 0. Verified current state (all anchors re-checked at HEAD)

| Fact | Anchor |
|---|---|
| **An ungoverned message-action surface already ships**: hardcoded 翻譯/摘要/公文 in the SPA | `apps/anila-shell/src/app.jsx:1470-1482` (`DEFAULT_MESSAGE_ACTIONS`, `runMessageAction`) |
| It renders in the assistant action row, gated `!classified`, between 倒讚 and the OW-1 pager | `apps/anila-shell/src/chat.jsx:786-806`; props `messageActions`/`onAction` at `:306-307`; wired `app.jsx:2310-2311` |
| A second, server-side declarative precedent: agent functions `kind='prompt_action'`, `config={"template": …}` | `services/csp/app/api/agents/functions.py:22-40`; model `app/models/agent_prompt.py`; SPA fetch `app.jsx:492-512` |
| Today's execution = client-side `template.replace(/\{content\}/g, msg.text)` then `sendMessage(prompt)` — appended as a **new user turn** | `app.jsx:1477-1482` |
| Action row layout: copy / regenerate popover / 👍 / 👎 / **actions** / SiblingPager / delete-branch / AuditWatermark | `chat.jsx:688-837` |
| Regenerate popover = the floating-picker idiom OW-3 needs | `chat.jsx:719-767`; close effect `:328-334` |
| OW-1 sibling-branch write path, permission-gated, capped, ANILALM-excluded | `POST /{cid}/messages/{mid}/branch` → `api/conversations.py:491-516` → `conversation_service.py:360-423` |
| Client-side branch persist helper reusable verbatim | `runtime/messageTree.js:214-235` (`persistRegeneratedAssistant`); caller `app.jsx:1674-1723` |
| Conversation access gate (owner or admin-tier) | `conversation_service.py:212-217`, `:780-785` |
| ANILALM branch exclusion | `conversation_service.py:47-57` (`409 ANILALM 對話不支援訊息分支`) |
| Model dispatch gates the SPA already rides: conversation-access, memory latch, attachment inject, ceiling, usage | `api/proxy.py:736-749`, `:798-811`, `:1014-1025`; ceiling `services/proxy/ceiling.py:140-185` |
| Four-level contract + OE-4 two lines | `schemas/contracts/classification.py:39-43`, `outbound_action_allowed:89-95`, `classification_audit_required:98-103` |
| Frontend `classified` ≡ `level >= 密` ⇒ existing `!classified` gate **is already** `outbound_action_allowed` | `models/conversation.py:54-60`; `runtime/classified.js:43-74` |
| Audit fail-soft by default; `commit=False` + return-check = fail-closed idiom (P1.4) | `services/audit_service.py:24-63`; `api/users.py:476-509` |
| Audit metadata + IP are **owner-only** on read | `api/audit_logs.py:13-35` (`SENSITIVE_REDACTED`) |
| Audit table has **no** append-only/hash chain (G2 → PLAN 2.7, pending) | `models/audit_log.py:7-23` |
| Owner-only mutation + admin-tier read policy shape | `api/trusted_hosts.py:1-12, 48-74`; `auth_service.py:120-171` |
| Department-subtree scope resolution (flat load, no recursive SQL) | `services/department_tree.py:88-100`; consumer `unit_admin_service.py:60-70` |
| Binding-table pattern: FK + partial unique index + soft-revoke + service caps | `models/unit_admin_assignment.py:27-79`; migration `r1_0010:37-70` |
| **No project entity exists**; `ServiceProjectBinding.project_id` is free-form and its cluster is OE-2 D2 retirement | `models/registered_service.py:148-171`; `PLAN.md:213` |
| SSRF guard that in-process exec would bypass | `packages/anila-core/src/anila_core/security/url_guard.py:73-108` |
| Tests: SQLite `create_all`; frontend vitest | `services/csp/tests/conftest.py:38-60`; `apps/anila-shell/package.json:10` |
| Governance console owner-gated route mechanism | `apps/csp-governance-ui/src/router/index.js:178` (`requiresOwner`); nav `components/layout/AppSidebar.vue:89` |

**Driving consequences.** (a) The SPA owns message creation/dispatch → declarative = "server renders prompt, client dispatches through existing chat path" → rides every gate **by construction**. (b) exec grants capability nobody has today → server-side, off by default, separately auditable. (c) OW-1's sibling-branch primitive is the fillback surface.

## 1. Settled design

### Q1 — Data model & versioning
**Two tables + mutable row with `version` + `body_sha256`; full-snapshot audit rows in `audit_logs`. No versions table, no invocation table.**

```
message_actions
  id, name (unique), label, icon, kind('declarative'|'exec'),
  result_mode('to_model'|'direct'), body (Text), body_sha256 (char64),
  choices (JSONB/JSON), notes (Text), version (int, default 1),
  is_enabled (bool), created_by_user_id, updated_by_user_id,
  created_at, updated_at

message_action_bindings
  id, action_id (FK CASCADE), scope_type('role'|'department'|'user'),
  role (nullable), department_id (FK CASCADE, nullable),
  user_id (FK CASCADE, nullable), created_by, created_at
  CHECK: exactly one target column populated, matching scope_type
  3 partial unique indexes (one per scope_type)
```

- **Visibility = union of bindings, fail-closed** (no bindings ⇒ nobody). `department` covers the subtree via `get_descendant_ids_many(db, {dept_id}, include_self=True)`. Owner sees everything; **admin does NOT bypass**. `users.role`/`auth_service.py` untouched (P1.3 precedent).
- **Versioning**: version bump + audit snapshot; `body_sha256` on row AND in every audit row → silent DB edits detectable.
- Rejected: immutable versions table (second ledger, OE-2 anti-pattern; rollback = owner re-pastes from export). Rejected: reuse `agent_functions` (agent-scoped, dev/admin-writable, no bindings/snapshots; its contract says config is never executed server-side). Left untouched; convergence = follow-up.

### Q2 — Declarative execution path
**Server renders the prompt and returns it; SPA dispatches through the existing `/v1/chat/completions` streaming path against the conversation's current target.**
- Substitution = plain `str.replace` of `{content}` (target assistant message text), `{choice}` (choice's prompt), `{input}` (free text). **Never str.format / f-strings / Jinja** (injection surface; zero new deps).
- Model = what the user would get typing it themselves (`assistantMsg.routedAgentId || selectedAgentId`). No per-action pinning. Security argument: a declarative action grants no capability the presser lacks.
- Client sends `X-ANILA-Conversation-Id` → conversation-access, latch, attachment inject, ceiling, usage all apply unchanged. **Zero new gate code.**
- Rejected: server-side model call inside invoke (duplicates ~200 lines of proxy wiring = second dispatch path; kills streaming). Honest limitation: audit records the server-rendered prompt; a hostile client could send another prompt — but that's just "user typed something else", already permitted and metered.

### Q3 — Exec execution path (owner ruling: same-process)
- Contract: body defines top-level `def run(ctx) -> str`. Save-time `compile()` + `ast` walk for `FunctionDef run` — **never executes at save**.
- `ctx` = plain dict, no ORM/Session (thread-safety): `{message, message_id, conversation_id, choice, input, user:{id, username, department_id}}`.
- Runtime: `asyncio.wait_for(asyncio.to_thread(...), timeout=ANILA_ACTION_EXEC_TIMEOUT_SECONDS)` behind `asyncio.Semaphore(ANILA_ACTION_EXEC_MAX_CONCURRENCY)`. Compiled cache keyed `(action_id, version)`.
- **Honest capability statement (feeds the risk doc verbatim):** runs inside the csp FastAPI process as the container user, full stdlib + all installed packages; can read `os.environ` (DB URL, `MODEL_GATEWAY_API_KEY`, `CSP_SERVICE_TOKEN`, JWT key paths), read files incl. `secrets/*.pem` (`:ro` still readable), open its own DB connection as the app role (bypassing API-layer scoping), import/mutate `app.*`, make arbitrary outbound network calls **bypassing `url_guard.validate_outbound_url`** (SSRF allow-list), spawn subprocesses, monkey-patch the process. **No sandbox, no seccomp, no separate interpreter, no resource limits beyond wall-clock timeout + output cap. Whoever can author an exec action is equivalent to whoever can deploy code to the host.**
- **Anti-claims in code comments and UI**: timeout cannot kill the thread (CPython); on timeout → 504 + audit row, thread keeps running (semaphore bounds the leak). UI never says 沙箱/已隔離/已終止; wording 「逾時，已停止等待（背景可能仍在執行）」.
- `ANILA_ENABLE_ACTION_EXEC: bool = False` fail-closed. Off ⇒ exec actions filtered from `/visible`, invoke → 404 (no oracle); owner console lists with warning badge. Mirrors P0.2 flag-domain precedent; P2 scan answer = unreachable unless a documented flag covered by the signed risk acceptance is set.
- Output must be `str` (else `502 動作回傳值必須是字串`); truncated at `ANILA_ACTION_OUTPUT_MAX_CHARS` with `truncated: true`.
- Exceptions: user gets generic zh-TW message + `invocation_id`; full traceback → owner-only audit metadata.
- Rejected: subprocess + rlimit + dropped env — a half-sandbox that invites misplaced trust; recorded in risk doc as the upgrade path (single-file exec module = the seam).

### Q4 — Result fillback
**Assistant sibling branch of the target message via existing `POST .../branch`.** Pager shows `1/2`; cap/ANILALM/delete/rating/active-leaf all work; zero new write path. Provenance: `metadata.action = {id,name,version,kind,choice_id}`, `agent_name = "action:<name>"`; exec direct results get a 「自訂動作產出」 badge (not a model answer — 誠實原則). Rejected: appended turn (transcript pollution — today's behavior); in-place annotation/panel (loses 回填 semantics + tooling).

### Q5 — Picker UX
One icon button per visible action; floating panel above listing `choices[]`; reuse regenerate popover idiom (`chat.jsx:719-767`, `:328-334`), inline input for `input:true` choices. Choices in `message_actions.choices` JSON: `[{"id","label","prompt","input","input_label"}]`. Validation: ≤20 items; id `^[a-z0-9_-]{1,40}$` unique; label ≤60; prompt ≤4000; input_label ≤40. Fast paths: 0 choices → fire immediately; 1 choice input:false → fire immediately.

### Q6 — API surface
Prefix `/api/message-actions`. Mutations owner-only; management reads admin-tier with body redacted; user surface bound-scoped. Contract §4.

### Q7 — Frontend
Fetch `/visible` once per session; render in existing slot (`chat.jsx:789-806`); declarative streams, exec blocks (spinner placeholder); both end at `persistRegeneratedAssistant` + `refreshActivePath`. **Delete `DEFAULT_MESSAGE_ACTIONS`** — the three templates move to the runbook for the owner to paste (real author, real audit, real bindings). Agent `prompt_action` functions unchanged (convergence note).

### Q8 — Risk acceptance document
Outline §8 → `docs/security/ow3-exec-risk-acceptance.md`, drafted by WP-A implementer, owner signs before P2 scan.

### Q9 — Companion #2 mechanics (audit)
One book, fail-closed authoring, write-ahead invoke, owner-only ndjson export.

| Event | action | semantics |
|---|---|---|
| create/update/delete action | `message_action_create/_update/_delete` | `commit=False` + return-check → None ⇒ 500 + rollback, same transaction (P1.4). Metadata: full body snapshot, choices, `body_sha256`, previous sha, version, bindings |
| replace bindings | `message_action_bindings_replace` | same; before/after sets |
| successful invoke | `message_action_invoke` | committed **before** dispatch (durable intent); None ⇒ 500, nothing runs. Metadata: version + body_sha256 + choice + conv/message ids + conversation level + rendered-prompt sha256/length (not the body) |
| refused invoke | `message_action_invoke_refused` | committed before the gate HTTPException when a meaningful attempt hits a real conversation (classification / access_denied / not_branchable). Distinct action name from success; status+outcome=`refused`. Input-validation 4xx and indistinguishable 404s leave no row. |
| exec outcome | `message_action_exec_result` | committed after; **fail-soft** (side effect already happened) with logger.exception. Metadata: duration_ms, output_chars, truncated, error_type, traceback |
| export | `message_action_audit_export` | export itself is a 管理動作 (L275) |

Export: `GET /api/message-actions/audit/export?since=&until=` → `application/x-ndjson`, attachment, **owner-only**. Retention = platform half-year (no purge job exists platform-wide; not OW-3's to invent — §6). ⚠ Snapshot integrity rides on PLAN 2.7 (G2): until then tamper-EVIDENT at best — sentence must appear in the risk doc.

### Q10 — Sequencing
Three packages (§7). Deferred: project bindings (no project entity; ANILA-UI conversations have `collection_id = NULL` by contract).

### Classification interaction
- Invoke refuses when `not outbound_action_allowed(level)` (≥密) → 403 — server-side half of the existing UI gate (PLAN 2.4 shape); for exec it's substantive (exec can egress = 外流 face).
- Invoke success writes write-ahead `message_action_invoke`; classification (and access / not-branchable) refusals write separable `message_action_invoke_refused` — L242 satisfied for ≥營業秘密 on the success path; ≥密 attempts leave a refusal row for the privileged-insider threat model. Not unconditional: gates before the write-ahead still refuse without a success row.
- OW-3 writes **no** classification. Result inherits the conversation latch by construction; declarative rides normal proxy latch. Message-level classification columns stay unwritten.

## 2. File-by-file change list

### Create — backend (9)
- `services/csp/migrations/versions/r1_0013_message_actions.py` (§3)
- `services/csp/app/models/message_action.py` — `MessageAction`, `MessageActionBinding`; `JSONValue = JSON().with_variant(JSONB,"postgresql")`; `__table_args__` = CHECK + 3 partial unique indexes (idiom `unit_admin_assignment.py:29-38`)
- `services/csp/app/schemas/message_action.py` — `ActionKind`/`ResultMode` closed enums, `ALLOWED_ACTION_ICONS` frozenset, `ChoiceSpec`, Create/Update/Out/AdminOut, `BindingSpec`, `InvokeRequest`, `InvokeResponse`
- `services/csp/app/services/message_action_service.py` — CRUD+audit (fail-closed), `list_visible`, `resolve_for_invoke`, `render_template`, invoke orchestration
- `services/csp/app/services/message_action_exec.py` — **entire exec surface, one file**: `validate_source()` (compile+AST, no execution), compiled cache, `run_action(source, version, ctx)` thread+wait_for+semaphore+cap. Module docstring = Q3 capability statement verbatim + risk-doc pointer
- `services/csp/app/api/message_actions.py` — router (§4); import `_client_ip` from `app.api.agents._common:47`
- `services/csp/tests/test_message_actions.py` — §5 cases 1-20
- `services/csp/tests/test_message_action_exec.py` — §5 cases 21-28
- `docs/security/ow3-exec-risk-acceptance.md` — §8

### Modify — backend (3)
- `app/models/__init__.py` — imports + `__all__` (alphabetical after `Message`)
- `app/api/router.py` — include after `unit_admins_router` (line 49)
- `app/config.py` — six settings next to `ANILA_MESSAGE_MAX_SIBLINGS` (`:123-125`): `ANILA_ENABLE_ACTION_EXEC=False`, `ANILA_ACTION_EXEC_TIMEOUT_SECONDS=30`, `ANILA_ACTION_EXEC_MAX_CONCURRENCY=2`, `ANILA_ACTION_OUTPUT_MAX_CHARS=20000`, `ANILA_ACTION_INVOKE_PER_MIN=20`, `ANILA_ACTION_MAX_BODY_CHARS=20000`; zh-TW comments; exec flag comment cites risk doc + P0.2 precedent

### Create — anila-shell (2) [WP-B]
- `src/runtime/messageActions.js` — `listVisibleActions`, `invokeAction`, `ACTION_ICONS` map with fallback, `needsPicker`, `resolveChoice`, `buildActionMetadata`
- `src/__tests__/messageActions.test.js`

### Modify — anila-shell (2) [WP-B]
- `src/app.jsx` — delete `DEFAULT_MESSAGE_ACTIONS` (`:1470-1474`) + fallback (`:1475`); `customActions` state + fetch effect (model: `:492-503`, keyed on auth); rewrite `runMessageAction(msg, action, choice)`: guard auth+dbId+!isStreaming; `stopStreaming`; POST invoke; `outcome='prompt'` → placeholder sibling like regenerate (`:1579-1599`) + stream; `outcome='text'` → placeholder with text, no stream; `persistRegeneratedAssistant({...metadata: action})`; `refreshActivePath`; failures restore via `sanitizeRestoredMessages`. Pass `messageActions={customActions}` at `:2310`.
- `src/chat.jsx` — action-row block (`:786-806`): `ACTION_ICONS[action.icon]` in `IconButton`; `openActionId` state + popover (clone `:719-767`) listing choices; disabled when `isStreaming || classified`; 「自訂動作產出」 badge near `AuditWatermark` (`:830`) when `msg.metadata?.action` (map through `mapServerMessage`, `app.jsx:653-686`)

### Create/Modify — csp-governance-ui (4) [WP-C]
- Create `src/api/messageActions.js` (shape of `src/api/trustedHosts.js`)
- Create `src/views/MessageActionsView.vue` — list, editor (kind toggle, icon picker fed by `GET /icons`, body textarea with red exec warning banner quoting the capability statement, choices editor, bindings editor: role/department-tree/user), export button
- Modify `src/router/index.js` — route after trusted-hosts (`:119-122`), `meta:{requiresOwner:true}` (guard `:178`)
- Modify `src/components/layout/AppSidebar.vue:89` — nav 「自訂動作」, owner-only

## 3. Migration r1_0013 sketch
`revision="r1_0013"`, `down_revision="r1_0012"`. Template: `r1_0010` raw-SQL idempotent idiom.
upgrade(): (1) `CREATE TABLE IF NOT EXISTS message_actions (...)` with FKs (created_by/updated_by → users SET NULL) + CHECKs (kind IN ('declarative','exec'), result_mode IN ('to_model','direct')); (2) unique index on name; (3) `message_action_bindings` with FKs (action CASCADE, department CASCADE, user CASCADE, created_by SET NULL) + shape CHECK (exactly-one-target per scope_type); (4) index on action_id; (5) **three partial unique indexes** (`(action_id, role) WHERE scope_type='role'` etc. — NULLs are distinct in PG, so one composite unique would NOT dedupe; same reasoning as `ix_unit_admin_assignments_active_pair`).
**No seed data** — the three legacy buttons go in the runbook as paste-ready declarative bodies (real author, real audit rows, real bindings).
downgrade(): drop indexes then tables, IF EXISTS.
Docstring: SQLite gets these via create_all (partial-index predicates PG-only, r1_0009/r1_0010 note); `body` deliberately not encrypted at rest (owner-authored source; the threat model is the author, not the DB reader — cross-ref risk doc).

## 4. API contract
Common: JWT cookie/Bearer; CSRF double-submit on mutations (`middleware/csrf.py`); plain zh-TW `detail` (no envelope — OW-1 §4 pinned).

Owner/admin surface:
| Verb | Path | Auth | Notes |
|---|---|---|---|
| GET | `/api/message-actions` | require_admin | owner sees body; non-owner `body: "<owner-only>"` (SENSITIVE_REDACTED idiom) |
| GET | `/api/message-actions/icons` | require_owner | `{icons, action_exec_enabled}` — icon allow-list plus platform exec flag for the owner console |
| POST | `/api/message-actions` | require_owner | 201; validates kind/result_mode/icon/choices; exec → validate_source(); create-snapshot audit fail-closed |
| PUT | `/api/message-actions/{id}` | require_owner | version+=1, sha recomputed; update-snapshot audit |
| DELETE | `/api/message-actions/{id}` | require_owner | 204; final-state snapshot; bindings cascade |
| GET | `/api/message-actions/{id}/bindings` | require_admin | |
| PUT | `/api/message-actions/{id}/bindings` | require_owner | whole-set replace, before/after audit |
| GET | `/api/message-actions/audit/export` | require_owner | `?since=&until=&limit=` → x-ndjson attachment; full metadata; writes export audit row |

Errors: `400 未知的動作類型 '…'（可用：declarative, exec）` / `400 未知的圖示 '…'` / `400 動作程式碼必須定義 run(ctx) 函式` / `400 動作程式碼語法錯誤（第 N 行）：…` / `400 選項數量超過上限（20）` / `400 名稱已存在` / `413 動作內容過大` / `403 需要 owner 權限` / `404 動作不存在`.

User surface:
`GET /api/message-actions/visible` → `[{id,name,label,icon,kind,result_mode,choices}]` — enabled + bound (role ∪ dept-subtree ∪ user) or owner; exec omitted when flag off; **body never appears**.
`POST /api/message-actions/{id}/invoke` `{conversation_id, message_id, choice_id, input}` → 200 `{invocation_id, action_id, version, kind, outcome:'prompt'|'text', prompt, output, truncated, duration_ms}`. declarative ⇒ always 'prompt'; exec ⇒ 'prompt' when result_mode='to_model' (return value IS the prompt), 'text' when 'direct'.

| Status | detail | Trigger |
|---|---|---|
| 404 | 動作不存在 | unknown/disabled/not-visible/exec-flag-off — one indistinguishable code (P1.3 invisibility rule) |
| 429 | 動作呼叫過於頻繁，請稍候再試 | per-user fixed window immediately after action resolution (before any refusal audit; 429 unrecorded; per-process; documented) |
| 404 | 找不到此對話 | get_conversation |
| 403 | 無權存取此對話 | _check_access |
| 400 | 訊息不屬於此對話 / 只能對助理訊息執行動作 | target validation |
| 403 | 此對話密等為「密」，不可執行自訂動作 | not outbound_action_allowed(level) |
| 409 | ANILALM 對話不支援訊息分支 | _require_branchable pre-check (before any execution) |
| 400 | 此動作需要選擇一個項目 / 未知的選項 / 此選項需要輸入內容 | choice validation |
| 413 | 輸入內容過長 | input > 2000 chars |
| 503 | 平台忙碌中，請稍後再試 | semaphore exhausted (short acquire timeout) |
| 504 | 動作執行逾時（30 秒），已停止等待；背景可能仍在執行 | TimeoutError — honest wording |
| 502 | 動作執行失敗（代號 {invocation_id}），請聯繫平台管理員 | exec exception; traceback → owner-only audit |
| 500 | 稽核紀錄寫入失敗，動作未執行 | write-ahead audit returned None |

Unchanged: everything in `api/conversations.py` — zero new conversation endpoints; fillback reuses `/branch`.

## 5. Test plan

### Backend — test_message_actions.py
1. POST as user/admin → 403; owner → 201.
2. Create declarative → version=1, sha correct; create audit row with FULL body + same sha.
3. PUT body → version=2, new sha; update audit carries prev+new sha.
4. Unknown icon → 400; unknown kind → 400; 21 choices → 400; dup choice id → 400.
5. exec without top-level run → 400; syntax error → 400 with line number; **nothing executed** (module-level canary never fires).
6. Fail-closed authoring: monkeypatch log_audit_event → None; POST → 500 AND row absent.
7. /visible no bindings → [] for plain user; owner sees it.
8. Bind by user → that user sees it, another doesn't. **(PLAN literal)**
9. Bind department parent → grandchild-dept user sees it; sibling tree doesn't.
10. Bind role='developer' → only developers.
11. is_enabled=false → gone from /visible; invoke → 404.
12. invoke unbound → 404 (not 403).
13. invoke declarative → outcome='prompt'; {content}/{choice}/{input} substituted; braces in message content survive (no str.format).
14. invoke writes exactly one message_action_invoke row: version+sha, NOT body.
15. foreign conversation → 403; missing conv → 404; message from other conv → 400.
16. ANILALM conv → 409 before any execution/render.
17. conv at 密 → 403; at 營業秘密 → 200 AND audit row (L242).
18. user-role target → 400.
19. 21st call in window → 429.
20. Export: owner → x-ndjson lines with body snapshots; admin → 403; export writes audit row.

### Backend — test_message_action_exec.py
21. Flag off → absent from /visible, invoke 404.
22. Flag on → run(ctx) str → outcome per result_mode; ctx keys exact, no Session/ORM.
23. Non-str return → 502.
24. Over-cap output → truncated, truncated:true.
25. run raising → 502 generic; exec_result audit has status failure + traceback; traceback ABSENT from HTTP body.
26. Sleeping run, timeout=1 → 504 honest wording; exec_result records timeout.
27. Write-ahead: monkeypatch runner to raise SystemExit; invoke row STILL present.
28. Compiled cache invalidates on version bump.

### Frontend — messageActions.test.js (WP-B)
1. needsPicker truth table. 2. resolveChoice unknown → null. 3. buildActionMetadata shape. 4. ACTION_ICONS fallback never throws. 5. hidden when classified; disabled while streaming. 6. picker open/outside-close/select-close. 7. declarative flow: invokeAction then branchMessage, never updateMessage. 8. outcome 'text': branchMessage without chat completions. 9. failed invoke restores pre-action list + runtimeError.

### e2e (live, real cookie auth; csp has no curl)
1. Owner creates 「翻譯成英文」, binds user A only → A sees icon, B doesn't (hard reload). **(PLAN 驗證欄)**
2. A presses → popover → choice → streamed reply as SIBLING, pager 2/2, original one click away. **(回填)**
3. Hard reload → result persists as sibling.
4. Owner edits body → version 2; audit-logs shows create+update; export contains both bodies.
5. Department bind at 院 → 組-level user sees it; other 所 doesn't.
6. Flag off → exec invisible + 404; flag on (up -d, NOT docker restart) → visible, invokes, branches.
7. exec sleeping 60s, timeout 30 → 504 honest message; two audit rows.
8. Classify conv 密 → buttons vanish AND direct invoke → 403 (UI+API agree — PLAN 2.4 shape).
9. Every assertion checks Content-Type.
10. alembic upgrade head from r1_0012 + downgrade -1 both clean on live PG.

Baseline: 797/37/1/2 at HEAD fb63005; judge delta. Frontend gate = npm run build.

## 6. Explicitly out of scope
1. Project-scoped bindings (no project entity). 2. Agent-scoped applicability. 3. include_history actions (PLAN: prompt 模板＋這則訊息; extension = one boolean + client passes path). 4. Per-action model pinning. 5. Actions on user/system/tool messages or text selections. 6. Any real sandbox (upgrade path in risk doc; single-file module is the seam). 7. Killing a runaway thread (CPython can't). 8. Cluster-wide rate limiting. 9. Audit retention/purge job (platform-wide gap, separate ticket). 10. Append-only audit (PLAN 2.7 dependency). 11. Converging agent_functions prompt_action (follow-up). 12. Converging OW-2 permission bits (→2.4). 13. Chained/scheduled actions, out-of-conversation writes. 14. body encryption at rest; i18n.

## 7. Work packages & review focus
**WP-A backend + risk doc**: migration, 2 models, schemas, 2 services, router, config, __init__, ~28 tests, risk-doc draft. Frozen API contract.
Review focus (second AND third vote): (i) message_action_exec.py line-by-line — the whole new attack surface; (ii) write-ahead/fail-closed ordering incl. SystemExit test; (iii) visibility resolver — union, fail-closed zero-bindings, subtree, no admin bypass; (iv) classification gate uses outbound_action_allowed, not re-derived; (v) no message-level classification writes; (vi) risk-doc capability statement matches what code can reach — **underselling is a review failure**.
**WP-B anila-shell** (after A): placeholder/restore choreography (stopStreaming + sanitizeRestoredMessages); branchMessage only write; DEFAULT_MESSAGE_ACTIONS removal leaves no dangling fallback; no isolation claims in UI wording.
**WP-C governance console** (may lag B): requiresOwner route AND server-side require_owner; exec warning banner text; icon picker fed from GET /icons.
No A+B merge; no B+C merge. Coordination: WP-A carries the only migration; router.py/config.py/models/__init__.py edits are append-only.

## 8. Risk-acceptance document outline (docs/security/ow3-exec-risk-acceptance.md)
1. 決議與範圍 — owner 2026-07-29 ruling; accepted = owner-authored in-process Python; NOT accepted = admin/user/agent-authored. Exists because PLAN:135 requires it.
2. 威脅模型 — privileged insider (SYSTEM-MAP L280); headline: exec author ≡ host deployer.
3. 能力聲明（逐條，不得美化）— Q3 list verbatim + anti-claims (no sandbox/seccomp/limits; timeout doesn't kill).
4. 為什麼仍然接受 — owner validated use case; declarative covers the majority; air-gapped single-tenant, one-person operator = sole author.
5. 控制措施（可查證，指到程式碼）— require_owner; default-off flag; one-file exec surface; static save validation; timeout/semaphore/cap with exact guarantees; bindings fail-closed; ≥密 gate.
6. 稽核與複查 — seven audit actions; sha256 chain of custody; owner-only export; cadence = monthly + after every authoring change; mismatch = incident.
7. 已知殘餘風險（必須寫）— (a) audit not append-only until 2.7 (tamper-evident at best); (b) timed-out thread keeps running; (c) per-process limits; (d) declarative audit records server-rendered prompt only; (e) exec egress bypasses SSRF allow-list.
8. P2 掃描對照表 — A03/CWE-94 (accepted here), A01 (mitigated), A09 (mitigated; residual until 2.7), A05 (default-off flag). Rows: 掃描項/對應程式碼/判定/依據.
9. 失效條件與回退 — re-review triggers (second author, non-owner authoring, multi-worker csp, security-centre isolation requirement); retreat = flag off → declarative keeps working → convert workflow to agent/n8n per PLAN mechanism table.
10. 簽署 — owner name, date, commit SHA, body_sha256 list of live exec actions.
