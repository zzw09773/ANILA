# OW-1 Implementation Blueprint — Message History Tree (branching)

> Authored by feature-dev:code-architect (opus), 2026-07-30. Saved verbatim by commander.
> Repo: `/home/c1147259/桌面/ANILA/anila-restart-20260729/ANILA`, branch `restart/from-redesign`. Alembic head `r1_0011` → new `r1_0012`.
> ⚠ Commander correction to §7 sequencing note: OE-4 DOES overlap on `api/conversations.py`, `services/conversation_service.py`, `api/public_share.py` (share gate/read-audit/search-audit sites). WP-A must be dispatched from the post-OE-4-merge HEAD.

## 0. Verified current state (all anchors re-checked in the main tree)

| Fact | Anchor |
|---|---|
| `Message` is linear: no `parent_id`, ordering by `created_at` only | `services/csp/app/models/message.py:10-43` (`created_at` at `:40`) |
| Conversation→messages relationship, `cascade="all, delete-orphan"`, `order_by="Message.created_at"` | `services/csp/app/models/conversation.py:65` |
| **Edit destroys the tail** — `DELETE FROM messages WHERE created_at > msg.created_at` | `services/csp/app/services/conversation_service.py:170-207` (delete at `:196-203`) |
| **Regenerate overwrites in place** (`update_message_content`) | `services/csp/app/services/conversation_service.py:210-255`, route `app/api/conversations.py:378-399` |
| Only message-creating path server-side | `conversation_service.append_message` at `:138-167`, route `app/api/conversations.py:343-359` |
| Ownership gate (admin-tier bypass) | `conversation_service.py:436-441` `_check_access` |
| ANILA/ANILALM distinguished by `conversations.origin` (`'anila-ui'`/`'anilalm'`/NULL=legacy ANILA) | `models/conversation.py:17-20`; filter semantics `conversation_service.py:109-118`; contract enforcement `api/conversations.py:215-233` |
| ANILALM writes messages only via `appendMessage` (never edit, never update) | `apps/anilalm/src/api/conversations.ts:66-73`; call sites `apps/anilalm/src/workspace/WSChat.tsx:252`, `:345` |
| Proxy does **not** write `messages` rows — the SPA does, after streaming | `app/api/proxy.py` (no `Message` import); SPA persists at `apps/anila-shell/src/app.jsx:1296-1312` |
| Attachments are **conversation-scoped**; injection filters on `conversation_id` only | `app/api/proxy.py:254-290`, esp. `:287`; budget doc `app/services/attachment_context.py:1-24` |
| `attachments.message_id` is `ON DELETE SET NULL`, `conversation_id` is `ON DELETE CASCADE` | `app/models/attachment.py:16-23` |
| Message-level classification columns exist but are **never written** | `models/message.py:29-39`; dispatch table `app/modules/policy/service.py:173-183`; latches `app/api/proxy.py:87-133` |
| Public share iterates **all** messages | `app/api/public_share.py:59-62` |
| Search joins any message content (branch-agnostic) | `app/api/conversations.py:275-283` |
| Client-only branch switcher (`revisions[].tail`), never persisted | `apps/anila-shell/src/app.jsx:1438-1503` (snapshot), `:1546-1564` (freeze), `:1607-1648` (`switchRevision`); UI pager `apps/anila-shell/src/chat.jsx:700-751`; wiring `app.jsx:2156-2174` |
| Tree-walking convention: one flat query + Python assembly, **no recursive SQL** | `app/services/department_tree.py:44-100` |
| Tests run on SQLite `create_all` (DB-level `ON DELETE` does **not** fire) | `services/csp/tests/conftest.py:38-60` |
| Frontend tests = vitest | `apps/anila-shell/package.json:10` |

Two consequences driving the design: (a) the **client** owns message creation, so `parent_id` must be threadable from the SPA and defaultable server-side for ANILALM; (b) **SQLite tests won't cascade**, so every cascade must also exist in Python.

## 1. Settled design

### Q1 — Tree representation
**Decision:** `messages.parent_id` (nullable self-FK, `ON DELETE CASCADE`) + `conversations.active_leaf_message_id` (nullable FK → `messages.id`, `ON DELETE SET NULL`). No `is_active` flag, no path materialization.

- **Linear view (common case), 2 queries, zero recursive SQL:** flat `(id, parent_id)` load → Python parent/children maps (idiom: `department_tree.py:44-56`) → walk up from `active_leaf_message_id`, reverse → hydrate exactly those ids. O(n).
- **Full tree (switcher):** same flat load yields per-fork sibling groups → `sibling_ids`/`sibling_index`/`sibling_count` on each `MessageOut`. ChatGPT-style pager needs **no** second endpoint. `?view=all` for admin/debug.
- **Rejected — per-message `is_active` (Open WebUI style):** switch = N UPDATEs; "exactly one active path" enforceable only by convention; half-failed write corrupts. Single pointer = one UPDATE; invalid state unrepresentable.
- **Rejected — path materialization (ltree/path string):** subtree delete/re-parent rewrites descendants; PG-specific; breaks SQLite test path; buys nothing at conversation scale.

### Q2 — Edit-re-ask
New user message is a **sibling** of the edited one (`parent_id = edited.parent_id`); edited row never mutated; subtree retained. `edit_user_message` (`conversation_service.py:170-207`) and route (`api/conversations.py:402-416`) are **deleted** (single caller: `apps/anila-shell/src/runtime/conversations.js:92`).
In-flight generation: server holds no streaming state. Client rule: branch-creating actions call `stopStreaming(convId)` (`app.jsx:459-462`) first. No server-side streaming lock.

### Q3 — Regenerate
New **assistant sibling** under the same user message. In-place overwrite path removed from ANILA regenerate (`app.jsx:1575-1581`); `PUT /messages/{id}` itself **stays** (ANILALM finalize + metadata patch).
Active path: every successful append/branch sets `active_leaf_message_id = new.id` unless `set_active=false`.
Sibling cap: `ANILA_MESSAGE_MAX_SIBLINGS: int = 20` in `app/config.py` (next to `ANILA_DEPARTMENT_MAX_DEPTH` `:118-121`). Exceed → 409.

### Q4 — Deletion
`DELETE /api/conversations/{cid}/messages/{mid}` = **subtree delete**, Python-side collect + bulk delete in one transaction; DB CASCADE as belt-and-braces only.
Algorithm: (1) flat load, collect subtree (cycle-safe); (2) sole-root guard → `409 對話至少需保留一則訊息;請改為刪除整個對話`; (3) if active leaf in subtree → repoint (parent, else newest surviving root, descend newest-child) **before** delete; (4) `UPDATE attachments SET message_id=NULL WHERE message_id IN subtree` (explicit — ORM delete-orphan on `Message.attachments` would wrongly DELETE attachment rows); (5) bulk delete (`synchronize_session=False`); (6) `log_audit_event(action="delete_message_branch", ..., metadata={"deleted_ids":[...],"count":n})`; (7) commit, return new active path.
**`delete_conversation` must change** (`conversation_service.py:130-134`): self-FK makes per-row ORM delete race the cascade. Null pointer → flush → null attachment message_ids → bulk delete messages → delete conv.

### Q5 — API shape
| Verb | Path | Purpose |
|---|---|---|
| POST | `/api/conversations/{cid}/messages` | extended: optional `parent_id`, `set_active` |
| POST | `/api/conversations/{cid}/messages/{mid}/branch` | new: sibling of `mid` (serves edit-re-ask AND regenerate) |
| PUT | `/api/conversations/{cid}/active-leaf` | new: switcher |
| DELETE | `/api/conversations/{cid}/messages/{mid}` | new: subtree delete |
| GET | `/api/conversations/{cid}?view=active\|all` | extended, default `active` |
| PUT | `/api/conversations/{cid}/messages/{mid}/edit` | **removed** |

One `/branch` endpoint for both ops; guard `body.role != target.role` → 400. `PUT /active-leaf` **canonicalizes**: accepts any message id, descends to newest leaf beneath it.

### Q6 — Classification/latch
No interaction; no change. Latching writes only conversation/task resources (`proxy.py:102-111,124-133`; `conversation_service.py:319-328`). OW-1 must NOT start writing message-level classification, must NOT weaken `canEdit = !classified` (`chat.jsx:274`), must NOT add new backend classification gates.
One in-scope fix: `public_share.py:59-62` must serve the **active path** (else share links expose abandoned branches).

### Q7 — Attachments/P1.5
Verified safe; no code change. Injection is conversation-scoped (`proxy.py:287`); identical on every branch. Subtree delete nulls `message_id`, preserves `conversation_id`. Deliberate: a document uploaded on branch B still enters the prompt on branch A (P1.5 conversation-scoping, `attachment_context.py:16-23`). Two regression tests mandatory.

### Q8 — ANILALM exclusion
Gate on `conversations.origin`: `_require_branchable(conv)` → `409 ANILALM 對話不支援訊息分支` for `/branch`, `PUT /active-leaf`, `DELETE .../messages/{mid}`, and `POST /messages` **only when `parent_id` explicitly supplied**. NULL origin = ANILA (matches `conversation_service.py:112-115`). Read paths not gated (linear ⇒ active==all).

### Q9 — Frontend
Rewire existing pager (`chat.jsx:700-751`) to server truth; delete `revisions[]`/`activeRev`/`tail` entirely. No migration needed (client state only; localStorage keys unrelated).
Touch list: `mapServerMessage` (`app.jsx:640-668`), hydration (`:734-757`), `handleEditUser` (`:926-1065`), `sendMessage` persist (`:1296-1312`), `regenerateMessage` (`:1415-1605`), `switchRevision`→`switchBranch` (`:1607-1648`), `MessageBubble` props (`:2156-2174`), `chat.jsx:233-248` + user-bubble pager (new, below `:445-447`) + delete-branch icon, API client (`runtime/conversations.js:60-115`).
**Unavoidable ordering change:** persist user message **before** streaming (today after, `app.jsx:1297-1300`) because assistant append needs real `parent_id`. Side benefit: fixes silent loss of user turn on failed persist.

### Q10 — Pagination/efficiency
No pagination in OW-1. Index `ix_messages_conversation_parent (conversation_id, parent_id)`; partial index `ix_conversations_active_leaf ON conversations (active_leaf_message_id) WHERE ... IS NOT NULL` (PG doesn't auto-index FKs; message deletes would seq-scan). **No recursive CTE anywhere** (keeps SQLite test path identical). Revisit trigger: >500 messages/conversation (write in docstring).

## 2. File-by-file change list

### Create (5)
- `services/csp/migrations/versions/r1_0012_message_tree.py` (§3)
- `services/csp/app/services/message_tree.py` — pure helpers: `load_edges`, `children_map`, `active_path_ids`, `descendants(include_self=True)`, `newest_leaf_under`, `sibling_groups`. Cycle-safe. Docstring cites `department_tree.py`, states "no recursive SQL".
- `services/csp/tests/test_message_tree.py` (§5)
- `apps/anila-shell/src/runtime/messageTree.js` — `deriveSiblingNav`, `neighbourId`, `applyServerPath` (unit-testable)
- `apps/anila-shell/src/__tests__/messageTree.test.js` (§5)

### Modify — backend (7)
**`models/message.py`** — `parent_id = Column(Integer, ForeignKey("messages.id", ondelete="CASCADE"), nullable=True)`; `__table_args__` += `Index("ix_messages_conversation_parent", "conversation_id", "parent_id")`, `CheckConstraint("parent_id IS NULL OR parent_id <> id", name="ck_messages_parent_not_self")`. Comment: `created_at` alone no longer a total order.
**`models/conversation.py`** — `active_leaf_message_id` FK SET NULL after `:26-30`; `:65` order_by → `(Message.created_at, Message.id)`. Comment: relationship is flat accessor, not user-facing rendering.
**`services/conversation_service.py`** — `append_message` new kwargs `parent_id`, `set_active=True` (None → default to active leaf; validate parent in-conv else `400 父訊息不屬於此對話`; sibling cap); delete `edit_user_message`; new `branch_message`, `set_active_leaf`, `delete_message_branch`, `_require_branchable`, `_load_active_path`; rewrite `delete_conversation`; `update_message_content` docstring: no longer regenerate path.
**`api/conversations.py`** — `MessageOut` += `parent_id`, `sibling_index`, `sibling_count`, `sibling_ids` (derived per response); `MessageAppend` += `parent_id`, `set_active`; new `MessageBranchCreate`, `ActiveLeafUpdate`, `ConversationPathOut`; `ConversationDetail` += `active_leaf_message_id`; `GET /{id}` `view` param; new branch/active-leaf/delete routes; delete `/edit` route.
**`api/public_share.py:59-62`** — iterate active path.
**`config.py`** — `ANILA_MESSAGE_MAX_SIBLINGS: int = 20`.
**`services/attachment_service.py`** — verify only.

### Modify — frontend (3)
**`runtime/conversations.js`** — appendMessage += parent_id/set_active; delete editUserMessage → `branchMessage`; new `setActiveLeaf`, `deleteMessageBranch`; `getConversation` accepts `{view}`.
**`app.jsx`** — per Q9 touch list; `exportConversation` (`:337-367`) unchanged + comment (exports active path, correct); `adoptColumn` (`:1814-1846`) unchanged + comment.
**`chat.jsx`** — props rename + `onDeleteBranch`; pager condition `msg.siblingCount > 1`, label `siblingIndex+1 / siblingCount`; keep streaming-disable (`:703-704`); NEW user-bubble pager; delete-branch IconButton gated `msg.siblingCount > 1 || msg.parentId != null` with confirm.

## 3. Migration r1_0012 sketch
`revision = "r1_0012"`, `down_revision = "r1_0011"`. Template: `r1_0009_department_tree.py` raw-SQL idempotent idiom.
upgrade(): (1) `ADD COLUMN IF NOT EXISTS parent_id INTEGER`; (2) FK `fk_messages_parent_id` → messages(id) ON DELETE CASCADE (pg_constraint-guarded); (3) CHECK `ck_messages_parent_not_self`; (4) `ix_messages_conversation_parent`; (5) conversations `active_leaf_message_id`; (6) FK SET NULL; (7) partial index `ix_conversations_active_leaf`; (8) backfill parent via `LAG(id) OVER (PARTITION BY conversation_id ORDER BY created_at, id)` updating only NULLs; (9) backfill active leaf = newest message per conversation.
Backfill rationale (docstring): needed for the `view=active` invariant on any pre-existing DB, not for data preservation.
No NOT NULL, no defaults (roots/new conversations legitimately NULL).
downgrade(): reverse drops, all IF EXISTS.
SQLite note in docstring (as r1_0009/r1_0010 do): partial index + window backfill are PG-path only; tests get columns via create_all.
Rejected: composite FK `(parent_id, conversation_id)` — redundant unique index + no repo precedent, for a violation a service check + test covers (OE-2 over-engineering context noted).

## 4. API contract
Common: JWT cookie auth, `_check_access` (owner or admin-tier → else `403 無權存取此對話`). Plain zh-TW `detail` errors; NO error-code envelope (no precedent; scope creep).

**GET `/api/conversations/{cid}?view=active|all`** → 200 ConversationDetail; `active_leaf_message_id`; messages each with `parent_id`, `sibling_index`, `sibling_count`, `sibling_ids`. view=all → every row ordered `(created_at, id)`. Errors: 403/404/422.

**POST `/{cid}/messages`** → 201 MessageOut. `parent_id` omitted/null → parent = active leaf (keeps ANILALM/legacy byte-identical). Errors: `400 父訊息不屬於此對話`, `400 父訊息不存在`, `409 ANILALM 對話不支援訊息分支` (only when parent_id explicit), `409 同一則訊息的變體已達上限（20）`, 413 metadata, 403, 404.

**POST `/{cid}/messages/{mid}/branch`** → 201 MessageOut. Server sets `parent_id = target.parent_id`, moves active leaf. Errors: `400 分支訊息的角色必須與原訊息相同`, `404 訊息不存在` (missing or foreign conv — same status, no probing), 409 ANILALM, 409 cap, 403.

**PUT `/{cid}/active-leaf`** `{message_id}` → 200 `ConversationPathOut {active_leaf_message_id, messages[]}`. Canonicalizes to newest leaf under target. Errors: 404, 409 ANILALM, 403, 422.

**DELETE `/{cid}/messages/{mid}`** → 200 ConversationPathOut (deviation from 204 convention is deliberate: client needs repointed path; follow-up GET would race a concurrent tab). Errors: 404, `409 對話至少需保留一則訊息;請改為刪除整個對話`, 409 ANILALM, 403.

**Removed:** `PUT /{cid}/messages/{mid}/edit`.
**Unchanged:** create/title/delete conv, `PUT messages/{mid}` (metadata patch), rating, classify, shares, search, public share (shape identical; content = active path).

## 5. Test plan

### Backend `tests/test_message_tree.py`
1. No-parent append chains to active leaf; N appends = linear chain, one leaf.
2. `/branch` on user message → same parent_id; original byte-identical; old subtree count unchanged.
3. `/branch` on assistant → two assistant children of one user message, sibling_count==2 both. **(PLAN literal criterion)**
4. view=active one branch; view=all both. (查得到)
5. active-leaf switch flips path; canonicalization descends when target has children. (可切換)
6. active-leaf with foreign-conv id → 404.
7. Cap: 21st variant 409; 20th 201.
8. parent_id from another conversation → 400.
9. Sibling ordering stable `(created_at, id)`.
10. Subtree delete removes node+descendants from view=all; sibling branch intact. (可各自刪除)
11. Deleting subtree containing active leaf repoints; never NULL/dangling.
12. Sole root delete 409; one of two roots 200.
13. delete_conversation on branched conv → no FK error, zero orphan messages/attachments.
14. Attachment survives branch delete: message_id NULL, conversation_id kept, row exists.
15. ANILALM: branch/active-leaf/delete → 409; plain POST (no parent_id) → 201.
16. Foreign conv → 403 on every new route; admin-tier allowed.
17. NULL-origin legacy conv → branch ops allowed.
18. Classify after branch B exists → branch A read 200; message rows' classification untouched.
19. Public share of branched conv = active path only; classified still zero messages.
20. `/search` still matches non-active-branch content — **pinned deliberately**.

### Frontend `__tests__/messageTree.test.js` (vitest)
1. deriveSiblingNav 2/3; neighbourId ±1 and null at ends.
2. Pager hidden when siblingCount<=1; prev disabled at 0; both disabled while streaming.
3. Pager renders on user bubbles when siblingCount>1 (new).
4. applyServerPath replaces list wholesale; preserves client-only fields (piiHits, explicitAgents) by dbId.
5. switchBranch calls PUT active-leaf and rebuilds from response; no optimistic mutation.
6. Regenerate never calls updateMessage; calls POST branch.

### e2e (live `-p anila-restart`, real cookie auth)
1. Login → new chat → Q1 → A1.
2. Regenerate → pager 2/2; view=all shows two assistant rows.
3. Switch to variant 1 → **hard reload** → still variant 1 (the criterion the client-only mechanism fails).
4. Edit Q1 → Q1′ → A2 under new sibling; user pager 2/2; switch back → A1 branch intact with rating.
5. Delete Q1′ subtree → gone from view=all; Q1 branch survives; active leaf real.
6. Every assertion checks Content-Type (SPA catch-all 200 text/html).
7. ANILALM: branch → 409; normal send 201.
8. Attachment: upload PDF, branch, switch, send on each → same 納入 count both branches.
9. `alembic upgrade head` from r1_0011 and `downgrade -1` both clean on live PG.

Baseline: 37 failed/1 skipped/2 errors pre-existing; judge delta.

## 6. Explicitly out of scope
1. Message-level classification (OE-3/OE-4 territory).
2. OW-2 per-action permission bits (→ 2.4).
3. Pagination/lazy loading (>500 msgs trigger).
4. Persisting compare mode (`adoptColumn` stays client-only).
5. Branch naming/merge/diff/graph visualisation.
6. ANILALM branching (SYSTEM-MAP:47 says no).
7. Per-branch attachment scoping (P1.5 deliberate).
8. Scoping `/search` to active branch (pinned by test 20).
9. Soft delete/undo/retention (hard delete + audit row).
10. Server-side streaming state/branch lock.
11. Editing assistant messages; branching from system/tool messages.
12. Re-parenting subtrees.
13. Conversation-level message cap.

## 7. Sizing / work packages
**WP-A — backend tree core** (one grok package): migration + models ×2 + message_tree.py + conversation_service + api/conversations + public_share + config + ~20 tests. Headlessly verifiable; ships frozen API contract.
**WP-B — frontend rewire** (one package, after WP-A merged): runtime/conversations.js + runtime/messageTree.js + app.jsx + chat.jsx + vitest. Risk = four intertwined stream handlers + persist-before-stream reordering.
Do not split further (edit vs regenerate share one endpoint/service/pager). Do not merge A+B (backend headless-testable, frontend not).
Coordination: whichever of OE-4/WP-A lands second confirms alembic head (OE-4 carries no migration — moot) and **rebases the actual overlapping files** (commander note at top); `app/modules/policy/service.py:177` "message" dispatch entry must not be touched.
Cross-family review: WP-A migration + delete semantics (esp. delete_conversation self-FK rewrite) get second and third vote; WP-B stream-handler reordering gets at least the second.
