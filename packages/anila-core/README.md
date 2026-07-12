# anila-core

> **ANILA Core** — Python agent runtime foundation（SDK）。ANILA 平台所有 agent 與 Router 共用的 in-process runtime 基座,外加整個後端 fleet 共用的 shared infrastructure。

> English mirror：[`README.en.md`](./README.en.md)。技術名詞、指令、程式碼一律保留英文。

> 🌿 **分支對照**:本 SDK 存在於所有 ANILA 部署分支,內容跨分支一致(runtime 基座不隨部署情境而異)。新功能一律先進 `main`,再 sync 進 downstream。分支策略見根目錄 [`README.md`](../../README.md) 與 [`docs/branch-sync-backlog.md`](../../docs/branch-sync-backlog.md)。

---

## 簡介

`anila-core` 是 ANILA 的 **Python runtime 基座**,定位為純 runtime — 不綁特定 RAG pipeline、不綁特定向量庫、不綁特定模型供應商。它承擔兩根支柱:

- **Pillar 1 · Agent runtime** — Router 與每支 agent 服務處理單一聊天 turn 所需的 in-process 元件(QueryEngine、Coordinator、providers、memory、tools、context、tracing、API server…)。
- **Pillar 2 · Shared infrastructure** — 不專屬於某個 agent process、被整個後端 fleet(Router、agent、ingestion-worker、未來各種 batch worker)共用的基底元件(credential crypto、SSRF url_guard、pg / pgvector adapters、chunking plugins、ingestion error taxonomy)。

平台各角色與 anila-core 的關係:

- **Router 部署**([`anila-core-router`](../../services/anila-core-router/)):直接 `import` Pillar 1 + Pillar 2。
- **Agent 開發者**:`anila-core init` 產生 non-RAG starter,或從 monorepo 以下方 local-path 指令安裝 `[rag]` 並 fork [`anila-agent`](../anila-agent/) 作為官方 RAG agent starter template。目前 internal distribution name 未在 public PyPI 發佈，不得直接 `pip install anila-core`。
- **ingestion-worker**(Arq 非同步 pipeline):只消費 Pillar 2(`chunking_plugins`、`IngestionError`、`pg_pool`、`pgvector_store`、`credential_crypto`),不碰 Pillar 1。

> **Redesign 後倉庫佈局(§17.1)**:monorepo 採 `services/`(可部署服務,含 `csp` / `anila-core-router`)、`apps/`(前端)、`packages/`(可 import 的套件,本 SDK 在此)、`infra/`(compose / 部署腳本 / nginx / models)四分層。根目錄 [`compose.yaml`](../../compose.yaml) 是 shim → `include: infra/compose/platform.yml`;部署腳本在 `infra/deployment/{scripts,intranet}/`。repo 根定位見 [`../../README.md`](../../README.md)。

---

## 架構與技術棧

- **語言**:Python `>=3.11`
- **建置**:`hatchling`(套件來源 `src/anila_core`)
- **套件名 / 版本**:`anila-core` v0.14.0(以 [`pyproject.toml`](./pyproject.toml) 為版本與 extras 的權威來源)

### Core dependencies(`pyproject.toml`)

| 套件 | 用途 |
|------|------|
| `pydantic>=2.0` / `pydantic-settings>=2.0` | DTO models + `Settings`(env 載入,欄位名直接對應 env var) |
| `fastapi>=0.110` / `uvicorn[standard]>=0.29` | API server(Router / agent server) |
| `httpx>=0.27` | provider 上游呼叫 + trace export |
| `sse-starlette>=2.0` | streaming(SSE turn 串流) |
| `python-frontmatter>=1.1` / `pyyaml>=6.0` | agent definition / 設定載入 |
| `anyio>=4.0` / `aiofiles>=23.0` | async IO |
| `asyncpg>=0.29` / `pgvector>=0.3` | Pillar 2 `CollectionScopedPgVectorStore`(ingestion 中央向量庫) |
| `anila-contracts>=0.1,<0.2` | 獨立的 Classification／StepEvent／AgentError wire contracts；`anila_core.contracts` 僅作 facade |
| `anila-security>=0.1,<0.2` | 舊 `anila_core.security` import 的相容 facade；實作與 `cryptography` 依賴在獨立套件 |
| `aiosqlite>=0.20` | 預設 `sqlite_session` short-term session adapter |

### Optional extras

- **`[rag]`** — 文件解析重量級堆疊:`pymupdf4llm` / `pymupdf` / `python-docx` / `odfpy` / `striprtf` / `Pillow`。重量級 parser / vision 實作落在 `anila_core.ingestion.parser_registry` 與 `anila_core.providers.vision`;`anila-agent/` 是純 starter template(無 production code 依賴)。
- **`[dev]`** — `pytest` / `pytest-asyncio` / `pytest-cov` / `respx` / `ruff` / `mypy`。

### Console script

```
anila-core = anila_core.cli.main:main   # init / register / status / agent bootstrap
```

---

## 目錄結構

`src/anila_core/` 關鍵模組(依 Pillar 分組):

```
packages/anila-core/
├── pyproject.toml            # name=anila-core, v0.14.0
├── README.md / README.en.md
├── CHANGELOG.md              # 詳細 sprint release notes(最新條目 v0.13.0)
├── e2e_smoke.py              # 手動 e2e smoke(需 OPENAI_API_KEY)
├── examples/                 # router-mode / simple-agent
├── tests/                    # pytest(testpaths=["tests"];integration/ 子套件需 live pgvector+RLS)
└── src/anila_core/
    ├── config.py             # Settings(pydantic-settings;CSP_BASE_URL / MODEL / API_DEV_MODE …)
    ├── app_factory.py        # FastAPI app factory
    │
    ├── ──── Pillar 1 · agent runtime ────
    ├── api/                  # server / router_server(create_router_app)+ events
    │   ├── session_owner.py · caller_context.py   # resume session→agent 表 + CallerContext(讀 X-ANILA-Task-Id)
    │   └── middleware/auth.py   # CSP service-token + rotating token
    ├── engine/               # query_engine(多階段 turn loop)+ budget_tracker
    │                         #   + approvals / guardrails / handoff / lifecycle
    ├── coordinator/          # multi-step decomposition + sub-agent dispatch
    ├── router/               # tool_router(ToolRegistry、plan-mode / permission gate)
    ├── tools/                # dispatch · ask_user · plan_mode · todo_write
    │                         #   · agent_as_tool · files · shell · apply_patch
    ├── providers/            # base · openai_compat · cspplatform_provider · vision · mock · embedding_mock
    ├── memory/               # short_term/(Session Protocol + in_memory / sqlite)
    │                         # long_term/(adapter · embedding · extraction · backends/{filesystem,postgres})
    │                         # + memdir · consolidation · relevance_selector · user
    ├── compact/              # micro / auto / session_memory / sliding_window
    ├── context/              # AgentContext(turn-scope contextvars,含 classified_latch)
    ├── post_turn/            # prompt_suggestion(follow-up chips)
    ├── tracing/              # span · tracer · processor · hooks
    │                         #   + sdk（anila_trace_sdk:TraceExporter / TraceSession / ExportingProcessor / SPAN_TYPES)
    ├── workspace/            # capability-scoped sandbox(workspace + caps + safe_path)
    ├── registry/             # agent_registry + remote_agent_manifest(從 CSP /v1/agents 撈)
    ├── runtime_config/       # snapshot · poller · apply(hot-reload)
    ├── models/               # pydantic DTOs
    ├── cli/                  # init / register / status / bootstrap + templates/
    │
    └── ──── Pillar 2 · shared infrastructure ────
        ├── security/         # anila-security 的向後相容 facade（無第二份狀態）
        ├── storage/          # ports.py(Protocol)+ adapters/(pg_pool · pgvector_store · memory_file_store)
        └── ingestion/        # errors(IngestionError taxonomy)· parser_registry · parsers · docling_parser
            │                 #   · ocr · citation_extractor · relation_resolution
            └── chunking_plugins/  # base · registry(@register_chunker)· builtins
```

> 完整模組責任與邊界見 [`../../docs/anila-core/anila-core-boundary.md`](../../docs/anila-core/anila-core-boundary.md)。

---

## Redesign 能力對應(anila-core 承擔的部分)

平台的 Slice 0–9 能力多數落在 CSP / 前端;anila-core 只提供其中的 **runtime 生產者面**。設計權威在 [`docs/anila-redesign-docs/`](../../docs/anila-redesign-docs/)(constitution `00`;runtime/registry 領域文件 `05`;凍結線協定 `09`)。

| 能力 | anila-core 承擔的面向 | 程式碼 / 文件 |
|---|---|---|
| **Full Trace**(spans + `/v1/traces` ingest) | `anila_trace_sdk` 生產者:批次 export span 到 CSP 端點,並 mirror 進 `anila.spans` SSE | `tracing/sdk.py`;doc `05` §6 / `09` §10 |
| **Task spine**(`X-ANILA-Task-Id`) | runtime 由 `CallerContext` 讀入並沿 turn 傳遞 task-id | `api/caller_context.py` |
| **五級分類 + 單向 latch** | agent runtime 守 per-turn classified 單向 latch(`ctx.classified_latch` → `anila_meta.classified`);`register` CLI 帶 `classification_ceiling`。**latch 執法 / 解密權威在 CSP** | `context/agent_context.py`;doc `08` |
| **Agent Registry**(7 態核准 + trace-test gate) | `register` / `status` CLI 送件進 CSP registry;`--draft` shadow 註冊。**核准態機與 trace-test gate 在 CSP** | `cli/register_cmd.py`;doc `05` |
| **Model Gateway**(`ANILA_ENV` http fail-closed) | `url_guard` 對 `endpoint_kind='model'` 在 production 硬拒 http(旗標救不了)。**per-model key / 5 態健康在 CSP** | `security/url_guard.py`;doc `04` §8 |

---

## 啟動與部署

anila-core 是 **library / SDK**,不是常駐服務。它被 Router / agent / ingestion-worker 以 import 消費,並透過 `create_router_app()` / `create_app()` 暴露 FastAPI app 供 uvicorn 啟動。

### 安裝與測試

```bash
pip install -e "./packages/anila-contracts" -e "./packages/anila-security" -e "./packages/anila-core[rag,dev]"
# 同一 invocation 提供全部 internal local candidates，避免 pip 向 public index 解析未保留名稱。

cd packages/anila-core
.venv/bin/python -m pytest            # asyncio_mode=auto;testpaths=["tests"]
.venv/bin/python -m pytest -m integration   # 需 live pgvector + RLS database
```

### Router 模式(OpenAI-compatible dispatcher)

```python
# main.py
from anila_core.api.router_server import create_router_app
app = create_router_app()
```

```bash
export CSP_BASE_URL=http://localhost:8000
uvicorn main:app --host 0.0.0.0 --port 9000
```

### 直接驅動一輪 QueryEngine

```python
from anila_core.engine.query_engine import QueryConfig, QueryEngine
from anila_core.providers.openai_compat import OpenAICompatProvider
from anila_core.router.tool_router import ToolRegistry
from anila_core.models.message import StreamDelta, UserMessage

provider = OpenAICompatProvider(base_url="http://csp:8000/v1", api_key="sk-...")
engine = QueryEngine(provider=provider, tool_registry=ToolRegistry(), config=QueryConfig())

async def on_delta(delta: StreamDelta) -> None:
    if delta.type == "text" and delta.text:
        print(delta.text, end="", flush=True)

# run() 跑完整輪迴並回傳 TurnResult;串流 delta 透過 on_stream_delta callback 送出
result = await engine.run([UserMessage(content="say hi")], on_stream_delta=on_delta)
print(result.stop_reason, result.turn_count)
```

> ⚠️ QueryEngine **沒有** `run_stream()`;入口是 `await engine.run(messages, on_stream_delta=...)`(見 [`e2e_smoke.py`](./e2e_smoke.py))。

### Full Trace 匯出(opt-in)

Tracing 是 **additive 且 fail-open**:`ANILA_TRACE_ENDPOINT` 未設 → 整條 trace 路徑 no-op,行為與未接前完全一致。設定後,span 由背景 `TraceExporter` POST 到 CSP `POST {base}/v1/traces/{trace_id}/spans`(body `{"spans":[…]}`,≤256/批,`Authorization: Bearer …` 認證),同時 mirror 進 `anila.spans` SSE 事件。

- `ANILA_TRACE_ENDPOINT`:bare flag(`1`/`true`/`on`/`yes`/`default`)→ 用 router 已知的 `CSP_BASE_URL`;其他值 → 當顯式 trace base URL。
- `ANILA_TRACE_TOKEN`:trace export 用的 service token(未設則 fallback `CSP_SERVICE_TOKEN`)。

程式面三件:`TraceExporter`(執行緒安全、批次、bounded queue、drop-and-log)、`TraceSession`(per-`trace_id` span factory,`span()` / `async_span()` context manager 自動計時 / 標 ok/error / auto-parent)、`ExportingProcessor`(把 in-tree `Tracer`/`Span` 橋接到 exporter,`SpanKind` → doc `05` §6 的 span-type)。皆從 `anila_core.tracing` 匯出。

### Scaffold 新 agent + 註冊

```bash
anila-core init my-agent      # 用 cli/templates/agent-template 產生 non-RAG starter
anila-core register \
  --csp http://localhost:8000 --endpoint http://your-host:9100 \
  --runtime-type anila_agent --classification-ceiling 機密 \
  --version 1.0.0 [--draft]
```

`register` 讀 `anila.yaml`、以 JWT 登入 CSP 後 `POST /api/agents/register`。新增旗標(Slice 5c;皆 override manifest、對閉集驗證):

| 旗標 | 說明 |
|---|---|
| `--runtime-type` | 5 值(doc `05` §3):`anila_agent` / `langchain` / `openwebui_pipe_compatible` / `openai_compatible_agent` / `custom_http` |
| `--classification-ceiling` | 五級(doc `08`):`無機密` / `營業秘密` / `機密` / `極機密` / `絕對機密` |
| `--version` | agent 版本字串(如 `1.0.0`) |
| `--draft` | shadow 註冊:僅治理中心可見,尚不可承接實際任務 |

---

## 安全:outbound URL guard(SSRF)

canonical API 是 `anila_security.validate_outbound_url(url, endpoint_kind="generic")`；`anila_core.security` 僅保留相同物件的相容 re-export。它是使用者提供之 endpoint URL 的中央 allow-list(CSP 建憑證時 + worker 呼叫時各驗一次,defense in depth)。**Slice 6a** 依 `endpoint_kind` 把 http 放寬旗標分域(僅影響 scheme;host / IP / DNS / trusted-host 檢查跨 kind 一致):

- **`model`** — production(`ANILA_ENV` ∈ {`production`,`prod`})一律 fail-closed 拒 http,`ANILA_ALLOW_HTTP_ENDPOINT` 救不了(doc `04` §8 硬規則);非 production 才吃該旗標。
- **`agent`** — http 由 `ANILA_ALLOW_HTTP_AGENT_ENDPOINT=1` 放行(內網 MLSteam 純 http NodePort agent);legacy `ANILA_ALLOW_HTTP_ENDPOINT` 仍作 deprecated fallback。
- **`generic`**(預設)— 既有全域語意,`ANILA_ALLOW_HTTP_ENDPOINT` 放行;既有呼叫端零行為變更。

host 面固定守則:deny list(loopback / `169.254.169.254` metadata / mDNS)、internal-zone 尾綴(`.internal` / `.local` / `.svc` …)、always-unsafe IP(loopback / link-local / multicast / reserved,硬拒)、RFC 1918 私網(`ANILA_ALLOW_PRIVATE_ENDPOINT=1` 才放行)、single-label 主機名一律拒。`ANILA_TRUSTED_HOSTS`(env)∪ DB-backed provider 是 admin allow-list,可略過 host 檢查(scheme 仍驗)。錯誤以 `UnsafeEndpointError`(帶 `reason` / `fixable_by_trust_host`)拋出。

---

## 與其他服務的關係

| 消費者 | 使用範圍 | 取得什麼 |
|--------|----------|----------|
| **anila-core-router** | Pillar 1 + Pillar 2 | `create_router_app()`、QueryEngine、Coordinator、`RemoteAgentRegistry`、service-token middleware、trace SDK |
| **anila-agent template**(fork 起點) | Pillar 1 + Pillar 2 + `[rag]` | 完整 runtime + 文件解析 / vision provider |
| **ingestion-worker**(Arq + Redis) | Pillar 2 + `anila-security` | core 的 ingestion/storage primitives；安全 primitive 直接從薄套件 import |
| **services/csp**(CSP backend) | `anila-security` + core 部分 | credential crypto／SSRF 直接依賴薄套件；其餘共用 runtime 仍來自 core |

---

## 相關文件

- Redesign 設計權威:[`../../docs/anila-redesign-docs/`](../../docs/anila-redesign-docs/) — constitution [`00`](../../docs/anila-redesign-docs/00-product-constitution.md)、runtime/registry 協定 [`05`](../../docs/anila-redesign-docs/05-agent-registry-and-runtime-protocol.md)、API/事件凍結線 [`09`](../../docs/anila-redesign-docs/09-api-event-contracts.md)、分類 latch [`08`](../../docs/anila-redesign-docs/08-classified-latch-and-policy-engine.md)、Model Gateway [`04`](../../docs/anila-redesign-docs/04-model-gateway-design.md)
- anila-core 邊界:[`../../docs/anila-core/anila-core-boundary.md`](../../docs/anila-core/anila-core-boundary.md) · runtime 設計:[`../../docs/anila-core/anila-core-runtime-design.md`](../../docs/anila-core/anila-core-runtime-design.md)
- Ingestion 平台設計:[`../../docs/ingestion/ingestion-platform-design.md`](../../docs/ingestion/ingestion-platform-design.md) · 詳細 release notes:[`CHANGELOG.md`](./CHANGELOG.md)
- 官方 RAG agent template:[`../anila-agent/README.md`](../anila-agent/README.md) · Router 薄殼:[`../../services/anila-core-router/README.md`](../../services/anila-core-router/README.md)
- 平台總覽:[`../../README.md`](../../README.md) · 分支策略:[`../../docs/branch-sync-backlog.md`](../../docs/branch-sync-backlog.md)

> 版本以 `pyproject.toml`(v0.14.0)為權威;`CHANGELOG.md` 最新條目為 v0.13.0。⚠️ 已知程式碼不一致(非本 README):`src/anila_core/__init__.py` 的 `__version__` 仍寫死 `"0.7.0"`,以程式方式讀 `anila_core.__version__` 會拿到舊值。
