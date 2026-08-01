# Slice 1B：CSP 骨架整理實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 依 doc 10 Slice 1「Refactor only: module layout / naming / contract schemas / tests organization」整理 CSP 骨架——行為零變更，為 Slice 2+ 的 modules 邊界鋪路。

**Architecture:** 三支 god-module 拆成 package（import surface 不變、route table 不變）；建 `app/modules/{tasks,policy,launch}` 邊界骨架＋import-linter 契約；建 `app/schemas/contracts/` 集中契約 schema（先只放 ClassificationLevel，其餘隨各 slice 進）。

**Tech Stack:** FastAPI router package 化、import-linter、pytest。

## Global Constraints

- 行為零變更：route table（method+path 集合）前後必須一致；pytest 失敗集合 ⊆ 1A 後基線（317P/43F/12E）。
- 不動 card auth 邏輯（`card_auth.py`/`card_auth_service.py` 不拆不改；`api/auth.py` 的 card 端點逐字搬移）。
- 不動 RLS / proxy 語意；`downstream_identity()` 員編語意逐字保留。
- 測試 monkeypatch 目標：拆分後 `app.api.agents`、`app.api.auth` namespace 必須 re-export 原符號；`proxy_service` 的 patch 目標改為新 module 路徑時，逐一驗證測試仍測到實作。
- doc 10 §17.2 的 tests 大型重組**不做**（churn>價值，記入計畫偏差），只修拆分引起的 import。

### Task 1: `app/api/agents.py`(1384) → `app/api/agents/` package

**Files:** Create `services/csp/app/api/agents/{__init__,_common,registration,approval,runtime_config,credentials,functions,health}.py`; Delete `services/csp/app/api/agents.py`
**Interfaces:** `from app.api import agents; agents.router` 不變；`__init__.py` re-export 全部原 module-level 符號（含測試 patch 目標）。
分工：registration（register/list/get/delete/template 下載）、approval（approve/reject）、runtime_config（get/patch + `/me/runtime-config` [HISTORICAL: removed]）、credentials（issue/rotate/revoke + dev db credential + encryption 切換）、functions（agent functions CRUD + system-prompt suggest）、health（health-check/test-connection）。共用 helper 進 `_common.py`。

- [ ] 逐字搬移（函式體不改）；`__init__` 組 router + re-exports
- [ ] `grep -rn 'app\.api\.agents' services/csp/tests` 確認 patch 目標仍有效
- [ ] route table 前後比對腳本通過；pytest 失敗集合 ⊆ 基線
- [ ] Commit `refactor(csp): split api/agents god-module (1384L) into package — 行為零變更`

### Task 2: `app/api/auth.py`(837) → `app/api/auth/` package

**Files:** Create `services/csp/app/api/auth/{__init__,password,oidc,card,revocations,registration_tokens}.py`; Delete `services/csp/app/api/auth.py`
card.py = challenge/verify 端點逐字搬移（安全不變量）。其餘同 Task 1 紀律。

- [ ] 搬移 + re-exports + route table 比對 + pytest gate
- [ ] Commit `refactor(csp): split api/auth god-module (837L) into package — card 端點逐字搬移`

### Task 3: `app/services/proxy_service.py`(893) → `app/services/proxy/` package

**Files:** Create `services/csp/app/services/proxy/{__init__,headers,sse,usage,guard,service}.py`；`proxy_service.py` 改薄 facade（re-export，維持既有 import 路徑）
headers=downstream_identity/build_agent_headers/build_model_gateway_headers/_resolve_outgoing_service_token；sse=_parse_sse_block/_aggregate_sse_to_chat_completion；usage=usage 序列化+估算；guard=_guard_outbound；service=proxy_request/proxy_stream。

- [ ] 搬移後全 repo grep 呼叫點與測試 patch 目標（`app.services.proxy_service.`）— call site 統一改新路徑或確保 facade 語意等價；有 patch 的測試逐一跑過
- [ ] pytest gate（proxy 相關測試檔全綠不退步）
- [ ] Commit `refactor(csp): split services/proxy_service god-module (893L) into package`

### Task 4: modules 邊界骨架 + lint-boundaries + contracts

**Files:** Create `services/csp/app/modules/{tasks,policy,launch}/__init__.py`（docstring 說明邊界）、`services/csp/app/schemas/contracts/{__init__,classification}.py`、`infra/ci/lint-boundaries.sh`、`services/csp/.importlinter`、`services/csp/tests/test_contract_classification.py`；Modify `services/csp/requirements.txt`（+import-linter，dev 段）
classification.py：`ClassificationLevel` 五級 enum（無機密<營業秘密<機密<極機密<絕對機密）＋ `max_level()`/比較運算＋與舊 boolean 映射 `from_legacy_classified()`（floor 語意）。import-linter 契約：modules 之間禁互 import 內部（doc 02 拍板）＋ doc 10 §12 import rules 可檢部分。

- [ ] 寫 enum 測試（排序、max、floor 映射）→ 實作 → 綠
- [ ] lint-boundaries.sh 本地跑通過
- [ ] Commit `feat(csp): modules boundary skeleton + ClassificationLevel contract + lint-boundaries gate`

## 驗收

- [ ] `services/csp` pytest：失敗集合 ⊆ 317P/43F/12E 基線
- [ ] route table diff 為空
- [ ] `bash infra/ci/lint-boundaries.sh` 通過
- [ ] 四個 commit 各自獨立可 revert
