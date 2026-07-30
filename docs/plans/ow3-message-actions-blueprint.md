# OW-3 Implementation Blueprint — Message-level Custom Action Buttons

> Delivered design (scope-reduced 2026-07-30 on `wt/ow3e-drop-exec`): **declarative prompt templates only**. The platform owner withdrew in-process Python execution; buttons open a floating picker and load a preset prompt that the client dispatches through the existing chat path. Create = developer+; update / delete / replace bindings = action author or administrator+; audit export = administrator+ with the same IP/metadata redaction as the platform audit listing (owner sees full rows).
>
> Spec basis: `PLAN.md` OW-3 block (owner updates PLAN separately). Depends on OW-1. Alembic head chain: `r1_0012` → `r1_0013` (tables) → `r1_0014` (drop kind/result_mode + delete obsolete exec rows).

## 0. Verified current state (anchors)

| Fact | Anchor |
|---|---|
| Ungoverned SPA hardcodes removed; actions come from `/api/message-actions/visible` | `apps/anila-shell` `customActions` + `runMessageAction` |
| Action row gated `!classified`, between 倒讚 and OW-1 pager | `apps/anila-shell/src/chat.jsx` |
| Fillback = OW-1 sibling branch | `POST .../messages/{mid}/branch` |
| Classification gate = `outbound_action_allowed` | invoke refuses ≥密 |
| Visibility = bound-to-caller **or** authored-by-caller; same rule for every role (no owner/admin bypass) | `message_action_service._user_visible_action_ids` |

## 1. Settled design (delivered)

### Q1 — Data model
**Two tables + mutable row with `version` + `body_sha256`; full-snapshot audit rows. No versions table, no invocation table. No kind / result_mode columns.**

```
message_actions
  id, name (unique), label, icon,
  body (Text), body_sha256 (char64),
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

- **Visibility = bound to the caller or authored by the caller** (same rule for every role; no owner/admin bypass). Fail-closed when neither matches. `department` covers the subtree.
- **Versioning**: version bump + audit snapshot; `body_sha256` on row AND in every audit row.
- **r1_0014**: deleted any `kind='exec'` rows (guarded by `information_schema` column existence — safe when the table was created from models without `kind`), then dropped `kind` and `result_mode` (single-valued structure after capability withdrawal).

### Q2 — Declarative path (the only path)
**Server renders the prompt and returns it; SPA dispatches through `/v1/chat/completions` against the conversation's current target.**
- Substitution = plain `str.replace` of `{content}` / `{choice}` / `{input}` only. Never str.format / f-strings / Jinja.
- Client sends `X-ANILA-Conversation-Id` → conversation-access, latch, attachment inject, ceiling, usage all apply. **Zero new gate code.**
- Invoke response: `{invocation_id, action_id, version, prompt}` — always a prompt for the client to stream.

### Q3 — Exec path — **withdrawn**
Owner withdrew pasted-Python execution. There is no execution module, no platform flag, no timeout/concurrency/output-cap settings, no `message_action_exec_result` audit action. The former risk-acceptance document under `docs/security/` was deleted (content preserved in git history); PLAN records why. Do not reintroduce an in-process exec surface without a new owner ruling.

### Q4 — Result fillback
**Assistant sibling branch of the target message via existing `POST .../branch`.** Provenance: `metadata.action = {id,name,version,choice_id}`, `agent_name = "action:<name>"`. Quiet action-name attribution in the shell; no 「自訂動作產出」 badge (that existed only to stop exec-direct results from passing as model answers).

### Q5 — Picker UX
One icon button per visible action; floating panel always opens on click (including zero-choice and single input-less shapes) so the presser can expand「查看將送出的內容」before sending. Zero-choice panels expose a「送出」control; choice rows keep their prior select/input behaviour. The disclosure region shows the raw template (placeholders marked, no client substitution preview), a caption stating server-side replacement of `{content}` / `{choice}` / `{input}`, and every choice's author-written prompt when present.

### Q6 — API surface & authorization
Prefix `/api/message-actions`. CSRF on mutations. Plain zh-TW `detail`.

| Verb | Path | Auth | Notes |
|---|---|---|---|
| GET | `/api/message-actions` | `_require_developer_or_admin` | `body` when caller may press (bound/authored **and** enabled) **or** may modify (author/admin-tier); strangers `null`. Admin-tier skips visibility-set load (modify short-circuits). Choice `prompt` text has always been present on this list for every developer viewer. |
| GET | `/api/message-actions/icons` | `_require_developer_or_admin` | `{icons, max_body_chars}` |
| POST | `/api/message-actions` | `_require_developer_or_admin` | 201; create-snapshot audit fail-closed |
| PUT | `/api/message-actions/{id}` | `_require_developer_or_admin` + author-or-admin | version+=1; update-snapshot audit |
| DELETE | `/api/message-actions/{id}` | `_require_developer_or_admin` + author-or-admin | 204; final-state snapshot |
| GET | `/api/message-actions/{id}/bindings` | `_require_developer_or_admin` + author-or-admin | |
| PUT | `/api/message-actions/{id}/bindings` | `_require_developer_or_admin` + author-or-admin | whole-set replace |
| GET | `/api/message-actions/audit/export` | `require_admin` | x-ndjson; reuses audit-listing redaction (`is_owner` / `<owner-only>`) |
| GET | `/api/message-actions/visible` | authenticated | bound-or-authored and enabled; **includes raw `body`** (pressable ⇒ readable) |
| POST | `/api/message-actions/{id}/invoke` | authenticated | write-ahead invoke audit; returns `{prompt}` |

Ownership = `created_by_user_id`. Author-or-admin refusals reuse the `require_admin` 403 shape. Export: admin+; non-owner viewers get redacted `ip_address` / `metadata` exactly as `GET /api/audit-logs`.

**Template read rule (OW-3f):** may-modify **or** pressable. Pressable = the same set `/visible` and invoke use (bound/authored **and** `is_enabled`). Bound-but-disabled therefore stops disclosing `body` on the management list to assignees who cannot modify. No identifier-oracle path: strangers still receive `body=null` / invisible rows; invoke keeps the indistinguishable 404.

**Accuracy note (do not overstate prior secrecy):** before OW-3f, management responses already returned each choice's full `prompt` to every developer viewer of the list — only the action `body` was redacted for non-authors. Describing the old behaviour as "strangers received nothing" is too generous; choice text was already visible to any developer who could open the management console. Plain users (non-developer) still had no management surface and no chat disclosure until OW-3f.

**Debt:** `/visible` now carries every pressable action's full template to every user at session start (when the shell loads custom actions). Acceptable under the transparency ruling; revisit if payload size or accidental logging becomes an issue.

### Q7 — Frontend
- **anila-shell**: fetch `/visible` (incl. `body`); always open picker; closed-by-default template disclosure + send for zero-choice; invoke → stream prompt → `persistRegeneratedAssistant` + `refreshActivePath`; restore on failure; classified gate; honest server-message passthrough; quiet `action:NAME` attribution.
- **csp-governance-ui**: route + sidebar `requiresDeveloper` (same pattern as knowledge collections / agents). No kind toggle, no exec warning, no flag badge, no result-mode field. Mutate UI (edit/bindings/delete) for author or admin-tier; export for admin+.

### Q8 — Risk acceptance
**Withdrawn with the capability.** Document deleted; history + PLAN hold the record.

### Q9 — Audit
| Event | action | semantics |
|---|---|---|
| create/update/delete | `message_action_create/_update/_delete` | fail-closed authoring |
| replace bindings | `message_action_bindings_replace` | before/after |
| successful invoke | `message_action_invoke` | write-ahead; version + body_sha256 + rendered-prompt sha/length (not body) |
| refused invoke | `message_action_invoke_refused` | classification / access_denied / not_branchable |
| export | `message_action_audit_export` | admin+; redaction matches audit listing |

### Classification
- Invoke refuses when `not outbound_action_allowed(level)` (≥密) → 403.
- OW-3 writes **no** classification. Result inherits the conversation latch.

## 2. Config
`ANILA_ACTION_INVOKE_PER_MIN` (default 20), `ANILA_ACTION_MAX_BODY_CHARS` (default 20000). No exec flag or exec tuning settings.

## 3. Explicitly out of scope
1. Any in-process / sandboxed code execution for message actions. 2. Project-scoped bindings. 3. Per-action model pinning. 4. Append-only audit (PLAN 2.7). 5. Converging agent `prompt_action` functions (follow-up).
