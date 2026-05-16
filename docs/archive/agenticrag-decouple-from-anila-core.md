# Phase 0 — Decouple AgenticRAG from anila-core

> **Status**: design draft, awaiting review
> **Date**: 2026-05-02
> **Purpose**: 把 `AgenticRAG/` 變成 ZERO-anila-core-import 的可 fork template。Devs `git clone AgenticRAG` 後不裝 anila-core 也能 standalone 跑。
> **Scope**: ~1000 LOC moved back + ~50 lines changed + 1 CI smoke test. 估 **3-5 個工作天**。
> **Non-goals**: 不動 anila-core 內部、不改 RAG 演算法、不上 Phase 0.5 / Phase 1 的內容。

## 為什麼

AgenticRAG 在 v0.6 / Phase 2 Sprint 1 Chunk F 時被當作「平台內部 RAG 服務」，所以把 `pg_pool` / `pgvector_store` 遷到 anila-core 中央 SDK，AgenticRAG 改 `import anila_core.storage.adapters`。

**現在定位變了** — AgenticRAG 是 dev 用來起新 agent 的 fork-template。Devs 不應該被迫安裝 platform-internal 套件。當前狀態：

| 檔案 | 違規程度 |
|---|---|
| `tools/__init__.py:31` | hard import `AgentScopedPgVectorStore` |
| `app_factory.py:44` | hard import `CollectionScopedPgVectorStore, PgPool` |
| `storage/adapters/__init__.py:28` | hard import `PgPool` |
| `storage/adapters/postgres_store.py:23` | hard import `PgPool` |
| `api.py:39-40` | hard import `CollectionScopedPgVectorStore, PgPool, SearchHit` |
| `Dockerfile` | build 階段 `pip install anila-core[bootstrap]` |
| `docker/entrypoint.sh:36` | 直接呼 `anila-core agent bootstrap` CLI |

**OK 的（loader pattern，留作典範）**：
- `api/middleware/loader.py` + `api/middleware/csp_auth.py`：try anila-core，fallback 本地

## 目標狀態

```
  AgenticRAG (template)                        anila-core (platform infra)
  ┌──────────────────────────┐                 ┌──────────────────────────┐
  │ storage/adapters/        │                 │ storage/adapters/        │
  │   pg_pool.py             │  ←── COPY ───── │   pg_pool.py             │
  │   pgvector_store.py      │  ←── COPY ───── │   pgvector_store.py      │
  │   memory_file_store.py   │                 │                          │
  │   postgres_store.py      │                 │                          │
  │ models/ingestion.py      │  ←── COPY ───── │ models/ingestion.py      │
  │   SearchHit              │                 │   SearchHit (canonical)  │
  │ storage/protocols.py     │  ★ NEW          │                          │
  │   VectorStore Protocol   │                 │                          │
  │ cli/bootstrap.py         │  ←── COPY ───── │ cli/bootstrap_cmd.py     │
  │ api/middleware/csp_auth  │                 │ api/middleware/auth.py   │
  │ + loader (fallback)      │                 │                          │
  └──────────────────────────┘                 └──────────────────────────┘
       ↑ self-contained                              ↑ platform admin only
       ↑ 0 imports from anila-core                   ↑ may import / wrap
       ↑ devs fork without surprises                   AgenticRAG's pieces
```

**承諾**：`pip install agentic-rag[rag]` → `docker compose up` → 跑得起來，**anila-core 完全不出現在 PYTHONPATH 都行**。

**Platform 內部部署不退步**：anila-core 仍然可選注入自己版本（透過 `app_factory` 的 vector store override hook，DI 形式而非 hard import）。

## 工作項目

### 1. 拷貝核心 storage 回 AgenticRAG

從 anila-core 把以下檔案複製到 AgenticRAG：

| Source（anila-core）| Destination（AgenticRAG）| LOC |
|---|---|---|
| `src/anila_core/storage/adapters/pg_pool.py` | `src/agentic_rag/storage/adapters/pg_pool.py` | ~105 |
| `src/anila_core/storage/adapters/pgvector_store.py` | `src/agentic_rag/storage/adapters/pgvector_store.py` | ~525 |
| `src/anila_core/models/ingestion.py` 的 `SearchHit` | `src/agentic_rag/models/ingestion.py` | ~30 |

**注意點**：

- `pgvector_store.py` 含上週做的 parent-child JOIN 邏輯（`add_parent_chunks` / `_attach_parent_content` / `chunk_type='leaf'` filter），整段照抄
- `SearchHit` 是 pydantic BaseModel，獨立沒下游依賴，最乾淨
- 若 anila-core 內部還想用 AgenticRAG 的版本當 source-of-truth，後續可改 anila-core 反向 re-export；**這份 plan 不負責**

### 2. 定義 VectorStore Protocol

新檔 `src/agentic_rag/storage/protocols.py`：

```python
from typing import Protocol, runtime_checkable

@runtime_checkable
class VectorStore(Protocol):
    """Minimum surface every vector store impl must satisfy.
    
    AgenticRAG only depends on this Protocol. The default impl is
    CollectionScopedPgVectorStore in agentic_rag.storage.adapters; 
    platform deployments inject anila-core's RLS-aware variant via 
    app_factory override.
    """
    async def add_chunks(self, chunks: list[Chunk]) -> list[int]: ...
    async def add_parent_chunks(self, chunks: list[Chunk]) -> dict[str, int]: ...
    async def index_chunks(self, ..., parent_id_map: dict[str, int] | None = None) -> ...: ...
    async def similarity_search(self, query_embedding, k: int) -> list[SearchHit]: ...
    async def keyword_search(self, query: str, k: int) -> list[SearchHit]: ...
    # ... 從現有 pgvector_store 抽 public method
```

**LOC**: ~60。

**為什麼 Protocol 而不是 ABC**：duck typing 對 fork 友好；devs 想換 Qdrant / Weaviate / Pinecone 不用繼承也能符合 Protocol。

### 3. app_factory 改 plugin 注入

`src/agentic_rag/app_factory.py:44` 目前：

```python
from anila_core.storage.adapters import CollectionScopedPgVectorStore, PgPool
# ...
vector_store = CollectionScopedPgVectorStore(pool, ...)
```

改成：

```python
# Default: AgenticRAG self-contained impl.
from agentic_rag.storage.adapters import CollectionScopedPgVectorStore, PgPool

# Optional override hook — platform deployments can inject 
# anila-core's RLS-aware variant via env or kwarg.
def build_vector_store(pool, *, override: VectorStore | None = None) -> VectorStore:
    if override is not None:
        return override
    return CollectionScopedPgVectorStore(pool, ...)
```

Platform deploy 在自己的 entrypoint 注入 anila-core 版：

```python
# csp-side wrapper around AgenticRAG (lives in CSP repo, not AgenticRAG)
from anila_core.storage.adapters import CollectionScopedPgVectorStore as AnilaCore
app = build_app(vector_store_override=AnilaCore(pool, ...))
```

**改動 LOC**: ~30 行（5 個 hard import 點 + 一個 builder function）。

### 4. Bootstrap CLI 本地化（option A 落地）

複製 `anila-core/src/anila_core/cli/bootstrap_cmd.py` （243 LOC）到 `src/agentic_rag/cli/bootstrap.py`，改名 + 修 entrypoint：

- CLI prog name：`anila-core agent bootstrap` → `python -m agentic_rag.cli bootstrap`
- 邏輯維持不變（POST `/api/agents/{id}/bootstrap`，state file 寫 0600）
- 新增 `src/agentic_rag/cli/__init__.py` + `__main__.py` 提供 `python -m agentic_rag.cli` 入口

`docker/entrypoint.sh:36` 改：

```bash
# Before
anila-core agent bootstrap --csp-url "$CSP_URL" --bootstrap-token "$BOOTSTRAP_TOKEN" \
    --agent-id "$ANILA_AGENT_ID" --endpoint-url "$AGENT_ENDPOINT_URL"

# After  
python -m agentic_rag.cli bootstrap --csp-url "$CSP_URL" --bootstrap-token "$BOOTSTRAP_TOKEN" \
    --agent-id "$ANILA_AGENT_ID" --endpoint-url "$AGENT_ENDPOINT_URL"
```

**LOC**: +180 (含 `__init__.py` / `__main__.py` boilerplate)。

**Drift 風險管理**：
- 在 `docs/csp-agent-bootstrap-protocol.md`（Phase 0.5 產出）freeze wire contract
- AgenticRAG 跟 anila-core 兩份 CLI 實作改 protocol 時都要同步；測試覆蓋兩份

### 5. Dockerfile 移除 anila-core build 步驟

```diff
- COPY anila-core /tmp/anila-core
- RUN pip install '/tmp/anila-core[bootstrap]'
```

**驗證**：移掉之後重 build image，跑 entrypoint 應該還是用得起 bootstrap CLI（因為複製到 agentic_rag.cli 了）。

### 6. README + template 文件更新

**`AgenticRAG/README.md`**：第一節 quick start 寫清楚：

```
# Quick start (standalone, no anila-core needed)
git clone <agenticrag-repo>
cd AgenticRAG
cp .env.example .env
echo "BOOTSTRAP_TOKEN=bsk-xxx" >> .env  # from CSP admin UI
docker compose up
```

**`AgenticRAG/templates/institutional-agentic-rag/README.md`**：移除任何 anila-core 提及。

**`AgenticRAG/docs/CSP_INTEGRATION.md`**：改寫，明確區分：
- **Standalone fork** — 不需要 anila-core，bootstrap CLI 自帶
- **Platform-flavored deployment** — 透過 vector_store override 用 anila-core 的 RLS 版

**`AgenticRAG/docs/BOOTSTRAP_DEPLOYMENT.md`**：更新所有指令從 `anila-core agent bootstrap` 改 `python -m agentic_rag.cli bootstrap`。

**LOC**: doc only, ~200 行改動。

### 7. CI smoke test：確保 ZERO-anila-core 啟動

新檔 `tests/test_no_anila_core_dep.py`：

```python
"""Smoke test: AgenticRAG must start without anila-core on PYTHONPATH.

Catches regressions where someone re-introduces a hard import.
"""
import importlib
import subprocess
import sys

def test_no_anila_core_imports_in_agentic_rag():
    """Static check: grep for anila_core imports in src/agentic_rag/."""
    result = subprocess.run(
        ["grep", "-r", "-n", "from anila_core\\|import anila_core",
         "src/agentic_rag/"],
        capture_output=True, text=True,
    )
    # Expected: only loader.py soft-import for middleware fallback.
    forbidden_lines = [
        line for line in result.stdout.splitlines()
        if "middleware/loader.py" not in line  # whitelist
        and "middleware/csp_auth.py" not in line
    ]
    assert not forbidden_lines, (
        f"AgenticRAG must not hard-import anila-core. Found:\n" +
        "\n".join(forbidden_lines)
    )

def test_app_factory_loads_without_anila_core(monkeypatch):
    """Ensure app_factory imports + builds with anila_core not installed."""
    # Hide anila_core from import system if present
    monkeypatch.setitem(sys.modules, "anila_core", None)
    # Force fresh import
    if "agentic_rag.app_factory" in sys.modules:
        importlib.reload(sys.modules["agentic_rag.app_factory"])
    from agentic_rag.app_factory import build_app
    # Should not raise ImportError
    assert build_app is not None
```

**LOC**: ~50（含 fixture）。

CI gate（GitHub Actions / 本地 pytest）：在 PR 時跑 `pytest tests/test_no_anila_core_dep.py -v`。任何 regression 直接 block。

## 實施順序（3-5 個工作天）

| 天 | 工作 | 驗證 |
|---|---|---|
| Day 1 | 1（拷貝 storage）+ 2（VectorStore Protocol）| `pytest tests/storage/` |
| Day 2 | 3（app_factory plugin 化）+ 5 個 hard import 改 | 啟動 standalone smoke test |
| Day 3 | 4（bootstrap CLI 搬家）+ Dockerfile 改 | 整套 docker compose up |
| Day 4 | 6（README / docs）+ 7（CI smoke test） | CI gate 過 |
| Day 5 | Buffer：跑 platform-flavored 部署、確認 vector_store override 路徑 OK；fix breakage | E2E E2E |

## 風險清單

| 風險 | Mitigation |
|---|---|
| anila-core 跟 AgenticRAG 兩份 pgvector_store drift | Drift detection：CI 跑 diff，差超過 N 行 fail；標記為 contract code |
| Platform deploy 不知道要 override，用了 AgenticRAG 自己 vector_store（少 RLS） | Logging：build_vector_store 在 platform env 偵測（CSP_INTERNAL=1）但沒注入 override 時印 WARN |
| Devs 不知道有 `[rag]` extra | README 第一句講清楚；`pyproject.toml` 加 deprecation warning if base install missing |
| Bootstrap CLI 兩份 drift | wire-contract 文件（Phase 0.5）+ 整合測試打到雙邊 |
| 漏抓某個 import | CI smoke test 抓得到，PR block |

## 驗收條件

- [ ] `grep -r 'from anila_core\|import anila_core' AgenticRAG/src/` 只剩 `middleware/loader.py` 跟 `middleware/csp_auth.py` 的 fallback path
- [ ] 在無 anila-core 環境 `pip install -e '.[rag]'` 然後 `python -m agentic_rag.api.server` 啟得起來
- [ ] `pytest tests/test_no_anila_core_dep.py` 過
- [ ] `docker build` 不依賴 anila-core
- [ ] `docker/entrypoint.sh` 用 `python -m agentic_rag.cli bootstrap` 跑通 happy path
- [ ] Platform deploy 用 vector_store override 還能跑（regression check）
- [ ] README + CSP_INTEGRATION.md + BOOTSTRAP_DEPLOYMENT.md 全更新

## 完成後解鎖什麼

- ✅ Devs 可以放心 fork AgenticRAG 起新 agent，不會撞到 anila-core 套件問題
- ✅ Phase 0.5（bootstrap protocol + dev page UX）有乾淨的程式碼可以引用
- ✅ Phase 1（7 個 RAG enhancement）整合點都不再 leak anila-core import

---

**Last updated**: 2026-05-02 · **Estimate**: 3-5 days · **LOC**: ~1000 moved + ~50 changed + ~50 test
