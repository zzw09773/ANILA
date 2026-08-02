> ⚠ **SUPERSEDED · 2026-08-01 P2.1**（後端發行面於 2026-08-02 Task 2+4 拆除）
> 本 runbook 描述的靜態 `csk-`／`bsk-`／`CSP_SERVICE_TOKEN` 上手與輪替路徑**已廢止**。
> 現行：平台派工 JWT＋JWKS 驗簽；開發者不保管長效 agent 祕密。
> 請改讀 `docs/guides/developer-guide.md`。下文僅供歷史對照，勿照做。
>
> **2026-08-02 後端狀態**（與上文「勿照做」對齊的活體契約）：
> - `POST /api/agents/{id}/issue-bootstrap` → **410**
> - `POST /api/agents/{id}/bootstrap` → **410**
> - `POST /api/agents/{id}/credentials/issue-static` → **410**
> - `POST /api/agents/{id}/credentials/{cid}/rotate` → **410**
> - `GET /api/agents/{id}/credentials/me` → **410**（Task 2：唯一尚存的 agent-kind 消費者；已無 Q19 前提）
> - `anila-core agent bootstrap` → **exit 1**（同樣訊息）
> - Admin `GET/DELETE .../credentials` 仍可用（確認空表／清 orphan）
> - 平台內部 `POST /api/service-clients/{id}/issue-static` **仍可用**（router／worker；不是 agent）

# Legacy / non-AgenticRAG agent bootstrap

> **HISTORICAL.** Sprint 8 X / Phase F. The three tiers below assumed agents
> held a long-lived `csk-` and presented `X-CSP-Service-Token` on inbound
> traffic. That wire protocol is gone for agents: CSP dispatch sends
> `Authorization: Bearer <5m RS256 JWT>`; agents verify via JWKS.
> Do not implement Tier 0 / 1 / 2 against a current CSP.

The former wire protocol (no longer live for agents):

```
X-CSP-Service-Token: <csk-...>   ← retired for agent dispatch
```

---

## Decision tree (historical)

```
                  ┌─ Can fork AgenticRAG and run anila-core?
                  │      └─ Tier 2 (full bootstrap, auto-rotate aware)  ← GONE
                  │
                  ├─ Can run a 50-line poller in your language?
                  │      └─ Tier 1 (admin issues bsk-, agent polls /credentials/me)  ← GONE
                  │
                  └─ Want zero code change, just env var swap?
                         └─ Tier 0 (admin issues static csk-, periodic
                            manual rotate)  ← GONE
```

**What to do instead:** register the agent (name + endpoint, no secret),
have an admin approve it, implement JWKS verification on
`POST /v1/chat/completions`. See `docs/guides/developer-guide.md`.

---

## Tier 0 / 1 / 2 — retired endpoints

The curl / CLI examples that used to live in this file targeted:

| Former call | Status since 2026-08-02 |
|---|---|
| `POST /api/agents/{id}/credentials/issue-static` | **410** |
| `POST /api/agents/{id}/credentials/{cid}/rotate` | **410** |
| `POST /api/agents/{id}/issue-bootstrap` | **410** |
| `POST /api/agents/{id}/bootstrap` | **410** |
| `GET /api/agents/{id}/credentials/me` | **410** |
| `anila-core agent bootstrap` | **exit 1** |

Reference snippets that verified inbound `X-CSP-Service-Token` (Python /
Go / Node) are obsolete for new agents. Keep them out of production
templates; the official path is JWKS verification of the dispatch JWT.

---

## Choosing your tier

| Concern | Former Tier 0–2 | Current (P2.1) |
|---|---|---|
| Agent-held long-lived secret | `csk-` in env / state file | **none** |
| Inbound auth | `X-CSP-Service-Token` | `Authorization: Bearer` dispatch JWT |
| Out-of-task CSP callbacks | (runtime-config poll / revocation — never shipped agent-side) | not needed |
| Registration | issue csk- in wizard | name + endpoint only |
