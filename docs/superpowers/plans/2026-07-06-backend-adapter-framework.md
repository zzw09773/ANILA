# Backend Adapter Framework (Phase 1+2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 csp model gateway 依 `model.protocol` 選 backend adapter，把各家推理後端統一成 OpenAI 相容；本計畫落地 Phase 1（adapter 骨架 + vLLM passthrough）+ Phase 2（llama.cpp adapter：passthrough + 首 token read timeout 放寬）。

**Architecture:** 新增 `adapters/` 套件（`BackendAdapter` Protocol + registry）。proxy 非串流（`_proxy_request_impl`）與串流（`proxy_stream`/`_proxy_stream_impl`）在轉發前後套對應 adapter；`openai_compatible` → passthrough（no-op，行為與現況逐位元組相同）。protocol 值域用 Pydantic `Literal` whitelist 擋在寫入前，只放已實作的值。

**Tech Stack:** Python 3.11、FastAPI、httpx、SQLAlchemy、Pydantic v2、pytest；前端 Vue 3（csp-governance-ui）。

## Global Constraints

- 只改 `services/csp/` 與 `apps/csp-governance-ui/`；不動 docker 容器；commit 只在 user 要求時（本計畫每 task 的 commit step 留給執行者依 user 指示）。
- venv：`services/csp/.venv/bin/python`（已建）；測試 `.venv/bin/python -m pytest`。
- passthrough 路徑（`protocol=openai_compatible`）行為**必須與現況逐位元組相同**——既有 csp 測試全綠不 regress（baseline：37 failed + 2 errors 既有，不得增加）。
- protocol whitelist（Phase 1+2）：`ModelCreate`/`ModelUpdate` 只接受 `Literal["openai_compatible", "llamacpp"]`；`ModelResponse.protocol` 維持裸 `str`（DB 殘值如舊 `custom_adapter` 不得炸 response）。
- 串流 hook 位置：`_proxy_stream_impl` 的 `async for line in resp.aiter_lines()`（service.py:574）每行進 `block_lines.append(line)`（service.py:600）**之前**套 `from_backend_stream_chunk`；passthrough 回原行 → block 組裝 / `[DONE]` holdback / anila.meta 注入 / usage 攔截全部吃未改動的行。
- TDD：每個行為先寫紅測試 → 驗紅 → 實作 → 驗綠。

---

## File Structure

- Create `services/csp/app/services/proxy/adapters/__init__.py` — registry + `get_adapter(protocol)`。
- Create `services/csp/app/services/proxy/adapters/base.py` — `BackendAdapter` Protocol + `PassthroughAdapter`。
- Create `services/csp/app/services/proxy/adapters/llamacpp.py` — `LlamaCppAdapter`。
- Modify `services/csp/app/services/proxy/service.py` — 非串流(154起) + 串流(473/729起) 接入 adapter。
- Modify `services/csp/app/schemas/model_registry.py:20,43` — protocol `Literal` whitelist（create/update）。
- Modify `services/csp/app/services/auto_seed.py:253-267` — 建立/更新帶 protocol。
- Modify `apps/csp-governance-ui/src/views/ModelsView.vue:215-218,318-322` — protocol 下拉移除 custom_adapter、只放 whitelist。
- Modify `docs/anila-redesign-docs/04-model-gateway-design.md:47` — 同步 protocol 值域。
- Create `services/csp/tests/test_backend_adapters.py`、`test_proxy_adapter_wiring.py`、`test_llamacpp_adapter.py`。

---

## Task 1: BackendAdapter 介面 + PassthroughAdapter

**Files:**
- Create: `services/csp/app/services/proxy/adapters/base.py`
- Test: `services/csp/tests/test_backend_adapters.py`

**Interfaces:**
- Produces: `BackendAdapter` Protocol（`name: str`、`request_timeout(endpoint_kind, default: httpx.Timeout) -> httpx.Timeout`、`backend_path(endpoint_kind, model_name, api_version) -> str`、`to_backend_request(endpoint_kind, body: dict) -> dict`、`from_backend_response(endpoint_kind, resp: dict) -> dict`、`from_backend_stream_chunk(endpoint_kind, raw_line: str) -> str | None`、`from_backend_error(endpoint_kind, status: int, raw_body: str) -> dict`）；`PassthroughAdapter`（`name="openai_compatible"`）。

- [ ] **Step 1: Write the failing test**

```python
# services/csp/tests/test_backend_adapters.py
import httpx
from app.services.proxy.adapters.base import PassthroughAdapter

def test_passthrough_is_noop():
    a = PassthroughAdapter()
    assert a.name == "openai_compatible"
    body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    assert a.to_backend_request("chat", body) is body
    resp = {"choices": [{"message": {"content": "x"}}]}
    assert a.from_backend_response("chat", resp) is resp
    assert a.from_backend_stream_chunk("chat", "data: {}") == "data: {}"
    assert a.backend_path("chat", "m", "v1") == "/v1/chat/completions"
    assert a.backend_path("embeddings", "m", "v1") == "/v1/embeddings"
    default = httpx.Timeout(30.0)
    assert a.request_timeout("chat", default) is default

def test_passthrough_error_openai_shape():
    a = PassthroughAdapter()
    out = a.from_backend_error("chat", 400, '{"error":{"message":"bad"}}')
    assert out == {"error": {"message": "bad"}}
    # 非 JSON body → 包成 OpenAI error
    out2 = a.from_backend_error("chat", 500, "upstream boom")
    assert out2["error"]["message"] == "upstream boom"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/csp && .venv/bin/python -m pytest tests/test_backend_adapters.py -q`
Expected: FAIL（ModuleNotFoundError: adapters.base）

- [ ] **Step 3: Write minimal implementation**

```python
# services/csp/app/services/proxy/adapters/base.py
from __future__ import annotations
import json
from typing import Protocol, runtime_checkable
import httpx

_V2 = ("/v1", "/v2")

@runtime_checkable
class BackendAdapter(Protocol):
    name: str
    def request_timeout(self, endpoint_kind: str, default: httpx.Timeout) -> httpx.Timeout: ...
    def backend_path(self, endpoint_kind: str, model_name: str, api_version: str) -> str: ...
    def to_backend_request(self, endpoint_kind: str, body: dict) -> dict: ...
    def from_backend_response(self, endpoint_kind: str, resp: dict) -> dict: ...
    def from_backend_stream_chunk(self, endpoint_kind: str, raw_line: str) -> str | None: ...
    def from_backend_error(self, endpoint_kind: str, status: int, raw_body: str) -> dict: ...

_KIND_PATH = {"chat": "chat/completions", "embeddings": "embeddings", "models": "models"}

class PassthroughAdapter:
    """openai_compatible：全 no-op，行為 == 現況。"""
    name = "openai_compatible"

    def request_timeout(self, endpoint_kind: str, default: httpx.Timeout) -> httpx.Timeout:
        return default

    def backend_path(self, endpoint_kind: str, model_name: str, api_version: str) -> str:
        return f"/{api_version}/{_KIND_PATH[endpoint_kind]}"

    def to_backend_request(self, endpoint_kind: str, body: dict) -> dict:
        return body

    def from_backend_response(self, endpoint_kind: str, resp: dict) -> dict:
        return resp

    def from_backend_stream_chunk(self, endpoint_kind: str, raw_line: str) -> str | None:
        return raw_line

    def from_backend_error(self, endpoint_kind: str, status: int, raw_body: str) -> dict:
        try:
            parsed = json.loads(raw_body)
            if isinstance(parsed, dict) and "error" in parsed:
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
        return {"error": {"message": raw_body[:500] or f"upstream status {status}"}}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/csp && .venv/bin/python -m pytest tests/test_backend_adapters.py -q`
Expected: PASS（2 passed）

- [ ] **Step 5: Commit**（依 user 指示）

```bash
git add services/csp/app/services/proxy/adapters/base.py services/csp/tests/test_backend_adapters.py
git commit -m "feat(csp): BackendAdapter 介面 + PassthroughAdapter"
```

---

## Task 2: adapter registry + get_adapter

**Files:**
- Create: `services/csp/app/services/proxy/adapters/__init__.py`
- Test: `services/csp/tests/test_backend_adapters.py`（append）

**Interfaces:**
- Consumes: `PassthroughAdapter`（Task 1）。
- Produces: `get_adapter(protocol: str) -> BackendAdapter`（未知值 → PassthroughAdapter + warn log）。

- [ ] **Step 1: Write the failing test**

```python
# append to services/csp/tests/test_backend_adapters.py
from app.services.proxy.adapters import get_adapter

def test_get_adapter_known_and_fallback(caplog):
    assert get_adapter("openai_compatible").name == "openai_compatible"
    # 未知/DB 殘值（如舊 custom_adapter）→ fallback passthrough + warn
    a = get_adapter("custom_adapter")
    assert a.name == "openai_compatible"
    assert any("custom_adapter" in r.message for r in caplog.records)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/csp && .venv/bin/python -m pytest tests/test_backend_adapters.py::test_get_adapter_known_and_fallback -q`
Expected: FAIL（ImportError: get_adapter）

- [ ] **Step 3: Write minimal implementation**

```python
# services/csp/app/services/proxy/adapters/__init__.py
from __future__ import annotations
import logging
from .base import BackendAdapter, PassthroughAdapter

logger = logging.getLogger(__name__)

_PASSTHROUGH = PassthroughAdapter()
_REGISTRY: dict[str, BackendAdapter] = {
    _PASSTHROUGH.name: _PASSTHROUGH,
}

def register(adapter: BackendAdapter) -> None:
    _REGISTRY[adapter.name] = adapter

def get_adapter(protocol: str | None) -> BackendAdapter:
    if protocol in _REGISTRY:
        return _REGISTRY[protocol]
    logger.warning(
        "未知 protocol %r（可能為 DB 殘值），fallback 至 openai_compatible passthrough",
        protocol,
    )
    return _PASSTHROUGH

__all__ = ["get_adapter", "register", "BackendAdapter", "PassthroughAdapter"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/csp && .venv/bin/python -m pytest tests/test_backend_adapters.py -q`
Expected: PASS（3 passed）

- [ ] **Step 5: Commit**（依 user 指示）

```bash
git add services/csp/app/services/proxy/adapters/__init__.py services/csp/tests/test_backend_adapters.py
git commit -m "feat(csp): adapter registry + get_adapter fallback"
```

---

## Task 3: protocol Literal whitelist（schema）

**Files:**
- Modify: `services/csp/app/schemas/model_registry.py:20,43`
- Test: `services/csp/tests/test_protocol_whitelist.py`

**Interfaces:**
- Produces: `ModelCreate.protocol` / `ModelUpdate.protocol` 型別 `Literal["openai_compatible","llamacpp"]`；`ModelResponse.protocol` 維持 `str`。

- [ ] **Step 1: Write the failing test**

```python
# services/csp/tests/test_protocol_whitelist.py
import pytest
from pydantic import ValidationError
from app.schemas.model_registry import ModelCreate, ModelResponse

_BASE = dict(name="m", display_name="M", model_type="llm", endpoint_url="http://x:8000")

def test_create_accepts_whitelisted():
    assert ModelCreate(**_BASE, protocol="llamacpp").protocol == "llamacpp"
    assert ModelCreate(**_BASE).protocol == "openai_compatible"  # 預設不變

def test_create_rejects_unimplemented_backend():
    with pytest.raises(ValidationError):
        ModelCreate(**_BASE, protocol="triton")  # Phase 1+2 未實作 → 422

def test_response_tolerates_db_residue():
    # DB 殘值（舊 custom_adapter）不得炸 response
    r = ModelResponse(
        id=1, name="m", display_name="M", model_type="llm",
        endpoint_url="http://x:8000", api_version="v1", is_active=True,
        health_status="healthy", health_checked_at=None, description=None,
        context_window=None, protocol="custom_adapter",
    )
    assert r.protocol == "custom_adapter"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/csp && .venv/bin/python -m pytest tests/test_protocol_whitelist.py -q`
Expected: FAIL（`test_create_rejects_unimplemented_backend`：目前 protocol 是 str，triton 不會被拒）

- [ ] **Step 3: Write minimal implementation**

在 `services/csp/app/schemas/model_registry.py` 頂部 import：
```python
from typing import Literal
```
把 `ModelCreate.protocol`（原 L20）改成：
```python
    protocol: Literal["openai_compatible", "llamacpp"] = "openai_compatible"
```
把 `ModelUpdate.protocol`（原 L43）改成：
```python
    protocol: Literal["openai_compatible", "llamacpp"] | None = None
```
`ModelResponse.protocol`（L71）**不動**（維持 `str = "openai_compatible"`）。

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/csp && .venv/bin/python -m pytest tests/test_protocol_whitelist.py -q`
Expected: PASS（3 passed）

- [ ] **Step 5: Commit**（依 user 指示）

```bash
git add services/csp/app/schemas/model_registry.py services/csp/tests/test_protocol_whitelist.py
git commit -m "feat(csp): protocol Literal whitelist（create/update），response 維持 str"
```

---

## Task 4: auto_seed 寫入 protocol

**Files:**
- Modify: `services/csp/app/services/auto_seed.py:253-267`
- Test: `services/csp/tests/test_auto_seed_protocol.py`

**Interfaces:**
- Consumes: AUTO_REGISTER_MODELS 的 model dict（可含 `protocol` key）。
- Produces: 新建 row 帶 `protocol=m.get("protocol","openai_compatible")`；既有 row 更新時同步 protocol。

- [ ] **Step 1: Write the failing test**

```python
# services/csp/tests/test_auto_seed_protocol.py — 用既有 conftest 的 db fixture
from app.models.model_registry import ModelRegistry

def test_seed_new_model_writes_protocol(db_session, monkeypatch):
    from app.services import auto_seed
    monkeypatch.setattr(auto_seed.settings, "AUTO_REGISTER_MODELS",
        '[{"name":"llama-x","endpoint_url":"http://172.16.120.35:18018","protocol":"llamacpp"}]')
    # ...呼叫 auto_seed 的 model 註冊路徑（照既有 test_auto_seed*/test_cli_register 的呼叫慣例）
    auto_seed.auto_seed()
    row = db_session.query(ModelRegistry).filter_by(name="llama-x").first()
    assert row.protocol == "llamacpp"

def test_seed_update_syncs_protocol(db_session, monkeypatch):
    from app.services import auto_seed
    db_session.add(ModelRegistry(name="llama-y", display_name="Y", model_type="llm",
        endpoint_url="http://old:8000", protocol="openai_compatible"))
    db_session.commit()
    monkeypatch.setattr(auto_seed.settings, "AUTO_REGISTER_MODELS",
        '[{"name":"llama-y","endpoint_url":"http://172.16.120.35:18018","protocol":"llamacpp"}]')
    auto_seed.auto_seed()
    row = db_session.query(ModelRegistry).filter_by(name="llama-y").first()
    assert row.protocol == "llamacpp"
```

> **實作者注意**：先讀 `services/csp/tests/test_cli_register.py` 與既有 auto_seed 測試，沿用它們建立 db_session + 呼叫 auto_seed 的 fixture 慣例；上面測試骨架可能需依實際 conftest 調整 session 注入方式。

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/csp && .venv/bin/python -m pytest tests/test_auto_seed_protocol.py -q`
Expected: FAIL（新建 row protocol 是預設 openai_compatible / 更新不改 protocol）

- [ ] **Step 3: Write minimal implementation**

`auto_seed.py` 新建 row（L253-261）加一行 `protocol`：
```python
                        model = ModelRegistry(
                            name=m["name"],
                            display_name=m.get("display_name", m["name"]),
                            model_type=m.get("model_type", "llm"),
                            endpoint_url=m["endpoint_url"],
                            api_version=m.get("api_version", "v1"),
                            description=m.get("description", ""),
                            context_window=m.get("context_window"),
                            protocol=m.get("protocol", "openai_compatible"),
                        )
```
更新分支（L264-267）改成同步 endpoint + protocol：
```python
                    else:
                        if existing.endpoint_url != m["endpoint_url"]:
                            existing.endpoint_url = m["endpoint_url"]
                            logger.info(f"更新模型端點: {m['name']} -> {m['endpoint_url']}")
                        new_proto = m.get("protocol")
                        if new_proto and existing.protocol != new_proto:
                            existing.protocol = new_proto
                            logger.info(f"更新模型 protocol: {m['name']} -> {new_proto}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/csp && .venv/bin/python -m pytest tests/test_auto_seed_protocol.py -q`
Expected: PASS（2 passed）

- [ ] **Step 5: Commit**（依 user 指示）

```bash
git add services/csp/app/services/auto_seed.py services/csp/tests/test_auto_seed_protocol.py
git commit -m "feat(csp): auto_seed 建立/更新寫入 protocol 欄位"
```

---

## Task 5: 非串流 proxy 接入 adapter

**Files:**
- Modify: `services/csp/app/services/proxy/service.py`（`_proxy_request_impl` ~154-260）
- Test: `services/csp/tests/test_proxy_adapter_wiring.py`

**Interfaces:**
- Consumes: `get_adapter`（Task 2）。
- Produces: 非串流路徑在打上游前套 `to_backend_request`、2xx 後套 `from_backend_response`、非 2xx 套 `from_backend_error`、timeout 用 `adapter.request_timeout`。

> **實作者注意**：先讀 `_proxy_request_impl` 全貌（service.py:130-260）確認 model 物件 / model_type / 上游呼叫與 `_get_timeout(model.model_type)`（:154）、error 解析（:248-258）、SSE 聚合（:267-268）的實際位置。openai_compatible 走 passthrough → 這些套用是 no-op，既有測試必須全綠。

- [ ] **Step 1: Write the failing test**

```python
# services/csp/tests/test_proxy_adapter_wiring.py
# 用 monkeypatch 注入一個記錄呼叫的假 adapter，斷言 _proxy_request_impl 有套用它。
# （骨架；依 service.py 的實際 httpx mock 慣例補完，參考 test_proxy_stream_usage.py）
import pytest

@pytest.mark.asyncio
async def test_nonstream_applies_adapter_to_request_and_response(monkeypatch):
    calls = {"req": 0, "resp": 0}
    class Spy:
        name = "openai_compatible"
        def request_timeout(self, k, d): return d
        def backend_path(self, k, m, v): return f"/{v}/chat/completions"
        def to_backend_request(self, k, b): calls["req"] += 1; return b
        def from_backend_response(self, k, r): calls["resp"] += 1; return r
        def from_backend_stream_chunk(self, k, l): return l
        def from_backend_error(self, k, s, b): return {"error": {"message": b}}
    monkeypatch.setattr("app.services.proxy.service.get_adapter", lambda p: Spy())
    # ...呼叫 _proxy_request_impl（openai_compatible model，mock 上游回 200 chat.completion）
    # 斷言 calls["req"] == 1 and calls["resp"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/csp && .venv/bin/python -m pytest tests/test_proxy_adapter_wiring.py -q`
Expected: FAIL（service 尚未 import/套 get_adapter）

- [ ] **Step 3: Write minimal implementation**

在 `service.py` import：`from app.services.proxy.adapters import get_adapter`。
在 `_proxy_request_impl` 內、取得 `model` 後：
```python
    adapter = get_adapter(getattr(model, "protocol", "openai_compatible"))
    request_body = adapter.to_backend_request("chat", request_body)  # embeddings 路徑用 "embeddings"
```
timeout（:154）：
```python
    base = httpx.Timeout(_get_timeout(model.model_type))
    timeout = adapter.request_timeout("chat", base)
```
2xx 回應解析後、回傳前套 `resp_json = adapter.from_backend_response("chat", resp_json)`（SSE 聚合分支則在 `_aggregate_sse_to_chat_completion` 之後套）；非 2xx error（:248-258）改用 `adapter.from_backend_error("chat", status, raw_text)`。endpoint_kind 依實際 endpoint（chat/embeddings）帶入。

- [ ] **Step 4: Run test to verify it passes + 全套不 regress**

Run: `cd services/csp && .venv/bin/python -m pytest tests/test_proxy_adapter_wiring.py -q && .venv/bin/python -m pytest -q`
Expected: 新測試 PASS；全套 failed 數 == baseline（37 failed + 2 errors），不得增加。

- [ ] **Step 5: Commit**（依 user 指示）

```bash
git add services/csp/app/services/proxy/service.py services/csp/tests/test_proxy_adapter_wiring.py
git commit -m "feat(csp): 非串流 proxy 接入 backend adapter（passthrough no-op）"
```

---

## Task 6: 串流 proxy 接入 adapter（proxy_stream + _proxy_stream_impl）

**Files:**
- Modify: `services/csp/app/services/proxy/service.py`（`proxy_stream` ~700-750、`_proxy_stream_impl` 473-600）、`services/csp/app/api/proxy.py`（agent 分支 ~620、model 分支 ~803）
- Test: `services/csp/tests/test_proxy_adapter_wiring.py`（append）

**Interfaces:**
- Consumes: `get_adapter`（Task 2）。
- Produces: `proxy_stream(..., protocol="openai_compatible")` 與 `_proxy_stream_impl(..., protocol="openai_compatible")` 新參數；串流每行套 `from_backend_stream_chunk`（回 None → 丟棄該行），timeout 用 `adapter.request_timeout`。

> **關鍵**：hook 在 `async for line in resp.aiter_lines()`（service.py:574）迴圈內、`block_lines.append(line)`（:600）**之前**。passthrough 回原行 → block 組裝（`_parse_sse_block`）、`[DONE]` holdback（:579-580）、anila.meta（:564-568）、usage 攔截（:590-594）全部吃未改動的行 → 逐位元組相同。`stream_options` 強制注入（:535）改成**先套 `to_backend_request("chat", request_body)` 再注入**。

- [ ] **Step 1: Write the failing test**

```python
# append to test_proxy_adapter_wiring.py
@pytest.mark.asyncio
async def test_stream_hook_passthrough_is_byte_identical(monkeypatch):
    """passthrough 串流輸出與未接 adapter 前逐位元組相同（含 [DONE]、多行 data、anila.meta）。"""
    # mock 上游 SSE：一個 chat.completion.chunk + usage chunk + [DONE]
    # 斷言 proxy_stream(protocol="openai_compatible") 的輸出 == 既有輸出快照
    ...

@pytest.mark.asyncio
async def test_stream_drop_line_when_adapter_returns_none(monkeypatch):
    class DropComments:
        name = "openai_compatible"
        def request_timeout(self, k, d): return d
        def from_backend_stream_chunk(self, k, line):
            return None if line.startswith(":") else line  # 丟棄 comment 行
        # 其餘方法同 passthrough
    monkeypatch.setattr("app.services.proxy.service.get_adapter", lambda p: DropComments())
    # mock 上游送一行 ": keepalive" + 正常 chunk；斷言輸出不含 keepalive
    ...
```

> **實作者注意**：串流測試的 httpx mock 慣例參考 `services/csp/tests/test_proxy_stream_usage.py`（已有 mock 上游 SSE 的手法）。

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/csp && .venv/bin/python -m pytest tests/test_proxy_adapter_wiring.py -k stream -q`
Expected: FAIL（proxy_stream 尚無 protocol 參數 / hook 未接）

- [ ] **Step 3: Write minimal implementation**

`_proxy_stream_impl` 簽名（service.py:473）末端加 `protocol: str = "openai_compatible"`。
`proxy_stream` wrapper（service.py:700 附近）簽名加 `protocol: str = "openai_compatible"`，並在呼叫 `_proxy_stream_impl(...)`（:729）帶入 `protocol=protocol`。
`_proxy_stream_impl` 內、串流開始前：
```python
    adapter = get_adapter(protocol)
```
`stream_options` 注入（:535）改：
```python
    body = adapter.to_backend_request("chat", request_body)
    body = {**body, "stream": True, "stream_options": {"include_usage": True}}
```
timeout（:547）改：
```python
        async with httpx.AsyncClient(
            timeout=adapter.request_timeout("chat", httpx.Timeout(settings.LLM_TIMEOUT))
        ) as client:
```
每行 hook（:574 迴圈內，`block_lines.append(line)` 之前）：
```python
                async for raw_line in resp.aiter_lines():
                    line = adapter.from_backend_stream_chunk("chat", raw_line)
                    if line is None:
                        continue
                    if line == "":
                        ...  # 既有 block 處理不變
                    else:
                        block_lines.append(line)
```
`api/proxy.py`：agent 分支（~620 呼叫 proxy_stream）帶 `protocol="openai_compatible"`；model 分支（~803）帶 `protocol=model.protocol`。

- [ ] **Step 4: Run test + 全套不 regress**

Run: `cd services/csp && .venv/bin/python -m pytest tests/test_proxy_adapter_wiring.py -q && .venv/bin/python -m pytest -q`
Expected: 新測試 PASS；全套 failed 數 == baseline，不增加（尤其 `test_proxy_stream_usage.py`、`test_proxy_task_wiring.py` 全綠）。

- [ ] **Step 5: Commit**（依 user 指示）

```bash
git add services/csp/app/services/proxy/service.py services/csp/app/api/proxy.py services/csp/tests/test_proxy_adapter_wiring.py
git commit -m "feat(csp): 串流 proxy 接入 adapter（per-line hook，passthrough 逐位元組相同）"
```

---

## Task 7: LlamaCppAdapter（passthrough + read timeout 放寬）

**Files:**
- Create: `services/csp/app/services/proxy/adapters/llamacpp.py`
- Modify: `services/csp/app/services/proxy/adapters/__init__.py`（註冊）、`services/csp/app/config.py`（`LLAMACPP_READ_TIMEOUT`）
- Test: `services/csp/tests/test_llamacpp_adapter.py`

**Interfaces:**
- Consumes: `PassthroughAdapter`（繼承）、`register`（Task 2）。
- Produces: `LlamaCppAdapter`（`name="llamacpp"`），`request_timeout` 回放寬 read 的 `httpx.Timeout`；其餘 passthrough。

- [ ] **Step 1: Write the failing test**

```python
# services/csp/tests/test_llamacpp_adapter.py
import httpx
from app.services.proxy.adapters import get_adapter
from app.services.proxy.adapters.llamacpp import LlamaCppAdapter

def test_llamacpp_registered():
    assert get_adapter("llamacpp").name == "llamacpp"

def test_llamacpp_widens_read_timeout_keeps_connect_short():
    a = LlamaCppAdapter()
    out = a.request_timeout("chat", httpx.Timeout(30.0))
    assert out.read >= 120.0        # read 放寬解冷啟
    assert out.connect <= 10.0      # connect 維持短，掛掉主機不吊滿

def test_llamacpp_chat_is_passthrough():
    a = LlamaCppAdapter()
    body = {"model": "gemma-4-31b", "messages": []}
    assert a.to_backend_request("chat", body) is body
    resp = {"choices": [], "usage": {"prompt_tokens": 1}}
    assert a.from_backend_response("chat", resp) is resp
    assert a.from_backend_stream_chunk("chat", "data: {}") == "data: {}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/csp && .venv/bin/python -m pytest tests/test_llamacpp_adapter.py -q`
Expected: FAIL（ModuleNotFoundError: adapters.llamacpp）

- [ ] **Step 3: Write minimal implementation**

`config.py` 加（`LLM_TIMEOUT` 附近，~L51-64）：
```python
    LLAMACPP_READ_TIMEOUT: float = 300.0  # llama.cpp 冷啟 load model 首 token 慢
```
`adapters/llamacpp.py`：
```python
from __future__ import annotations
import httpx
from app.config import settings
from .base import PassthroughAdapter

class LlamaCppAdapter(PassthroughAdapter):
    """llama.cpp：實測高度 OpenAI 相容（chat 完整+usage、error OpenAI 形狀、
    /v1/models 含 data 欄位）。唯一 override = read timeout 放寬解冷啟首 token。"""
    name = "llamacpp"

    def request_timeout(self, endpoint_kind: str, default: httpx.Timeout) -> httpx.Timeout:
        return httpx.Timeout(
            connect=min(default.connect or 10.0, 10.0),
            read=settings.LLAMACPP_READ_TIMEOUT,
            write=default.write or 10.0,
            pool=default.pool or 10.0,
        )
```
`adapters/__init__.py` 末尾註冊：
```python
from .llamacpp import LlamaCppAdapter
register(LlamaCppAdapter())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/csp && .venv/bin/python -m pytest tests/test_llamacpp_adapter.py -q`
Expected: PASS（3 passed）

- [ ] **Step 5: Commit**（依 user 指示）

```bash
git add services/csp/app/services/proxy/adapters/llamacpp.py services/csp/app/services/proxy/adapters/__init__.py services/csp/app/config.py services/csp/tests/test_llamacpp_adapter.py
git commit -m "feat(csp): LlamaCppAdapter（passthrough + read timeout 放寬）"
```

---

## Task 8: 前端 protocol 下拉

**Files:**
- Modify: `apps/csp-governance-ui/src/views/ModelsView.vue:215-218,318-322`

**Interfaces:**
- Produces: 註冊表單 `protocol` 下拉（`openai_compatible` / `llamacpp`），移除 `custom_adapter`。

> **實作者注意**：先讀 ModelsView.vue 的 `model_type` 下拉（今天新增 image 的那段）與現有 `protocol` 綁定（form.protocol 預設、L215-218 / L318-322）當範本。

- [ ] **Step 1: 改下拉選項**

把 protocol 下拉的 options 改成（移除 custom_adapter）：
```html
<select v-model="form.protocol" class="term-select">
  <option value="openai_compatible">openai_compatible</option>
  <option value="llamacpp">llamacpp</option>
</select>
```
`form.protocol` 預設維持 `"openai_compatible"`。既有列若 protocol 是 `custom_adapter`（DB 殘值），顯示時原樣呈現但下拉不提供該選項（存檔會被 server-side whitelist 驗證）。

- [ ] **Step 2: build 驗證**

Run: `cd apps/csp-governance-ui && npm run build`
Expected: build 成功（✓ built），無新錯誤。

- [ ] **Step 3: Commit**（依 user 指示）

```bash
git add apps/csp-governance-ui/src/views/ModelsView.vue
git commit -m "feat(governance-ui): protocol 下拉只放已實作 backend，移除 custom_adapter"
```

---

## Task 9: 同步 redesign doc SSOT

**Files:**
- Modify: `docs/anila-redesign-docs/04-model-gateway-design.md:47`

- [ ] **Step 1: 更新值域定義**

把 doc 04 §2 ModelEndpoint schema 的：
```
  protocol: "openai_compatible" | "custom_adapter"
```
改成：
```
  protocol: "openai_compatible" | "llamacpp" | "ollama" | "tensorrtllm" | "triton"
  # openai_compatible = passthrough（vLLM/trtllm-serve/原生相容）；其餘 = 對應 backend adapter。
  # 已實作：openai_compatible、llamacpp（Phase 1+2）。ollama/tensorrtllm/triton 隨 Phase 3-4 落地。
```

- [ ] **Step 2: Commit**（依 user 指示）

```bash
git add docs/anila-redesign-docs/04-model-gateway-design.md
git commit -m "docs(redesign): 同步 protocol 值域為 backend adapter id"
```

---

## Task 10（Acceptance，需進內網）: llama.cpp 真實 fixture 重放

> **不可在外網宣告完成。** 這是 Phase 2 的硬性驗收門檻（呼應本專案「只驗 health 是假陽性」教訓）。

- [ ] **Step 1: 內網錄製** 對 `172.16.120.35:18018`（gemma llama.cpp）錄製 chat 串流 + 非串流的真實 request/response，存 `services/csp/tests/fixtures/llamacpp_*.json`。
- [ ] **Step 2: 重放測試** 把 fixture 接進 `test_llamacpp_adapter.py`，斷言 adapter 處理後產出標準 OpenAI 形狀、串流逐塊正確、timeout 為放寬 read。
- [ ] **Step 3: 端到端** 在內網 stack 把一台 llama.cpp 註冊為 `protocol=llamacpp`、設 router primary，真跑一次 chat（串流）確認冷啟不逾時、輸出正確。
- [ ] **Step 4: 全套 csp 測試綠**（baseline 不增加）。

---

## 驗收總表（進 plan 完成的判準）

- Task 1-9：外網可完成，全套 csp 測試 failed 數 == baseline（37+2），新測試全綠，前端 build 綠。
- Task 10：進內網才可標完成；未做 = Phase 2「待內網驗收」。
- passthrough 路徑（openai_compatible）行為與現況逐位元組相同（Task 6 identity 測試佐證）。
