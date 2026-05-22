# FLUX.2-dev Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `black-forest-labs/FLUX.2-dev` 接成 ANILA 系統的一個 internal agent,軍方使用者只需在現有 chat UI 打字描述,系統自動派發到 FLUX 並把生成圖片 inline 顯示在對話中,完全不暴露 image-gen 參數。

**Architecture:** 不改 Router、不改 UI、不改 gemma4。新增兩個 internal-only container:`flux2-dev`(diffusers 推理引擎)+ `flux2-dev-agent`(OpenAI-compat shim)。在 CSP `AUTO_REGISTER_MODELS` 把後者註冊為 `model_type: agent`,讓 Router 的 `DISPATCH:image-generator:...` 派發機制自動接管。圖片落地到 `share-dev/uploads/flux/`,沿用既有 nginx `/uploads/` static route。`react-markdown` 預設就會 render `![](url)`,UI 不用改。

**Tech Stack:**
- 推理:Python 3.11、`diffusers` ≥ 0.32、PyTorch 2.4 CUDA 12.4、bf16、雙 H100 80GB(GPU 1 + GPU 2)
- Shim:Python 3.11、FastAPI、`httpx`、`pydantic`
- 容器:Docker Compose v2,沿用既有 `anila-models-net` external network
- 測試:`pytest`、`pytest-asyncio`、`respx`(httpx mock)
- 註冊:CSP `AUTO_REGISTER_MODELS` env JSON

**前置條件(非本計畫範圍,需先確認/完成):**

1. **License**:FLUX.2-dev 是 BFL Non-Commercial License,軍方部署要先取得商用授權,或確認屬非商用範圍。**沒解決這個就不要進 Task 1**。
2. **模型下載**:`huggingface-cli download black-forest-labs/FLUX.2-dev --local-dir /home/aia/c1147259/project/Huggingface/FLUX.2-dev`(約 110-120GB,沿用既有 HF 模型存放慣例)。
3. **External network 存在**:`docker network inspect anila-models-net`(現有 stack 已建立,理論上 OK)。

**檔案結構決策:**

```
models/
├── flux2-dev/                          # 新增:推理引擎
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── server.py                       # FastAPI: /health, /generate
│   └── tests/
│       ├── __init__.py
│       └── test_server.py              # 用 mock pipeline 測 endpoint shape
├── flux2-dev-agent/                    # 新增:OpenAI-compat shim
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py                     # FastAPI entrypoint
│   │   ├── chat_handler.py             # /v1/chat/completions 邏輯
│   │   ├── prompt_translator.py        # 中文→FLUX 英文(透過 gemma4 callback)
│   │   ├── flux_client.py              # HTTP client to flux2-dev:8000
│   │   ├── image_store.py              # 落地 PNG + URL 生成
│   │   └── schemas.py                  # pydantic models (OpenAI chat shape)
│   └── tests/
│       ├── __init__.py
│       ├── test_chat_handler.py
│       ├── test_prompt_translator.py
│       ├── test_flux_client.py
│       └── test_image_store.py
└── docker-compose.yml                  # 修改:加 flux2-dev + flux2-dev-agent

docker-compose-dev.yml                  # 修改:AUTO_REGISTER_MODELS 加 image-generator

share-dev/uploads/flux/                 # 新增目錄,bind-mount 給 agent
```

決策理由:
- 兩個 container 拆開,因為**推理引擎吃 GPU + 大記憶體**(model weights),**shim 是純 IO + 輕量 Python**(只跟 gemma4 / flux backend 講 HTTP)。把它們綁一起會浪費 shim 的可調度性,也讓 Dockerfile 變肥。
- `app/` 子目錄是因為 shim 有 4-5 個模組,直接攤平在頂層會亂。

**簡單組件邊界圖:**

```
┌──────────────────────────────────────────────────────────────┐
│                   anila-models-net network                    │
│                                                                │
│   ┌──────────────────────┐     ┌─────────────────────────┐   │
│   │  flux2-dev-agent     │     │  flux2-dev              │   │
│   │  :8000 (expose)      │ ──► │  :8000 (expose)         │   │
│   │  OpenAI compat shim  │     │  diffusers Flux2Pipeline │   │
│   │  GPU: none           │     │  GPU: ["1", "2"]        │   │
│   └──────────────────────┘     └─────────────────────────┘   │
│             │                                                  │
│             │ (POST gemma4 /v1/chat/completions for prompt    │
│             │  translation, via CSP proxy)                    │
│             ▼                                                  │
│   ┌────────────────────────────────────────────────────┐     │
│   │ csp:8000 (existing)                                 │     │
│   └────────────────────────────────────────────────────┘     │
└──────────────────────────────────────────────────────────────┘
        │
        │ images written here:
        ▼
share-dev/uploads/flux/<uuid>.png  ── nginx /uploads/flux/<uuid>.png ── UI <img>
```

---

## Task 1: flux2-dev-agent skeleton + schemas

**Files:**
- Create: `models/flux2-dev-agent/app/__init__.py`
- Create: `models/flux2-dev-agent/app/schemas.py`
- Create: `models/flux2-dev-agent/tests/__init__.py`
- Test: `models/flux2-dev-agent/tests/test_schemas.py`
- Create: `models/flux2-dev-agent/requirements.txt`
- Create: `models/flux2-dev-agent/pyproject.toml`

- [ ] **Step 1.1: Write the failing test for ChatRequest parsing**

Create `models/flux2-dev-agent/tests/test_schemas.py`:

```python
"""Schema tests — ensure the shim accepts OpenAI chat completion
requests in the shape that CSP forwards.

CSP forwards the body verbatim to the agent endpoint, so we must
accept the exact OpenAI v1 shape including the ``anila_session_id``
extension that anila-core's dispatch_tool.py embeds for stateful
conversations.
"""
from __future__ import annotations

import pytest

from app.schemas import ChatCompletionRequest, ChatMessage


def test_chat_request_minimal():
    req = ChatCompletionRequest(
        model="image-generator",
        messages=[ChatMessage(role="user", content="畫一張坦克")],
    )
    assert req.model == "image-generator"
    assert req.messages[0].content == "畫一張坦克"


def test_chat_request_with_anila_extension():
    req = ChatCompletionRequest(
        model="image-generator",
        messages=[ChatMessage(role="user", content="畫一張坦克")],
        anila_session_id="sess_abc123",
    )
    assert req.anila_session_id == "sess_abc123"


def test_chat_request_last_user_message_helper():
    req = ChatCompletionRequest(
        model="image-generator",
        messages=[
            ChatMessage(role="system", content="ignore me"),
            ChatMessage(role="user", content="first"),
            ChatMessage(role="assistant", content="ok"),
            ChatMessage(role="user", content="second"),
        ],
    )
    assert req.last_user_text() == "second"


def test_chat_request_rejects_empty_messages():
    with pytest.raises(ValueError):
        ChatCompletionRequest(model="image-generator", messages=[])
```

- [ ] **Step 1.2: Create requirements.txt**

Create `models/flux2-dev-agent/requirements.txt`:

```
fastapi==0.115.5
uvicorn[standard]==0.32.1
httpx==0.27.2
pydantic==2.9.2
python-multipart==0.0.12
```

- [ ] **Step 1.3: Create pyproject.toml(只給 pytest 用,容器內 runtime 用 requirements.txt)**

Create `models/flux2-dev-agent/pyproject.toml`:

```toml
[project]
name = "flux2-dev-agent"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "fastapi==0.115.5",
    "httpx==0.27.2",
    "pydantic==2.9.2",
]

[project.optional-dependencies]
test = [
    "pytest==8.3.3",
    "pytest-asyncio==0.24.0",
    "respx==0.21.1",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
pythonpath = ["."]
```

- [ ] **Step 1.4: Create empty `app/__init__.py` and `tests/__init__.py`**

Both empty. Just `touch` them.

- [ ] **Step 1.5: Run test to verify it fails(import error,schemas 還沒寫)**

```bash
cd /home/aia/c1147259/ANILA/models/flux2-dev-agent
python -m venv .venv
source .venv/bin/activate
pip install -e ".[test]"
pytest tests/test_schemas.py -v
```

Expected: `ModuleNotFoundError: No module named 'app.schemas'`

- [ ] **Step 1.6: Write minimal schemas.py**

Create `models/flux2-dev-agent/app/schemas.py`:

```python
"""Pydantic models matching the OpenAI v1 chat completion shape.

The shim accepts the exact body that anila-core's
``dispatch_to_agent_response`` sends, which is the standard OpenAI
``/v1/chat/completions`` body plus the ``anila_session_id`` extension
field (and optionally ``anila_handoff``). CSP forwards verbatim.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    stream: bool = False
    anila_session_id: Optional[str] = None
    anila_handoff: Optional[dict[str, Any]] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None

    @field_validator("messages")
    @classmethod
    def _non_empty(cls, v: list[ChatMessage]) -> list[ChatMessage]:
        if not v:
            raise ValueError("messages must not be empty")
        return v

    def last_user_text(self) -> str:
        for m in reversed(self.messages):
            if m.role == "user":
                return m.content
        raise ValueError("no user message in conversation")


class ChatCompletionChoice(BaseModel):
    index: int = 0
    message: ChatMessage
    finish_reason: str = "stop"


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[ChatCompletionChoice]
    usage: dict[str, int] = Field(
        default_factory=lambda: {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
    )
```

- [ ] **Step 1.7: Run test to verify it passes**

```bash
pytest tests/test_schemas.py -v
```

Expected: 4 passed.

- [ ] **Step 1.8: Commit**

```bash
git add models/flux2-dev-agent/
git commit -m "feat(flux2-dev-agent): schemas for OpenAI chat completion shape"
```

---

## Task 2: image_store(落地 PNG + URL 生成)

**Files:**
- Create: `models/flux2-dev-agent/app/image_store.py`
- Test: `models/flux2-dev-agent/tests/test_image_store.py`

`image_store` 把 FLUX 回傳的 PNG bytes 寫到 `/share/flux/<uuid>.png`,並回傳對外 URL `/uploads/flux/<uuid>.png`(對應 nginx `/uploads/` location)。隔離測試友善 + 邏輯簡單。

- [ ] **Step 2.1: Write the failing test**

Create `models/flux2-dev-agent/tests/test_image_store.py`:

```python
"""image_store: persist FLUX PNG output and compute public URL."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.image_store import ImageStore


def test_save_writes_png_and_returns_public_url(tmp_path: Path):
    store = ImageStore(
        local_dir=tmp_path,
        public_url_prefix="/uploads/flux",
    )

    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32  # PNG magic + garbage
    url = store.save(png_bytes)

    assert url.startswith("/uploads/flux/")
    assert url.endswith(".png")

    filename = url.rsplit("/", 1)[-1]
    written = tmp_path / filename
    assert written.exists()
    assert written.read_bytes() == png_bytes


def test_save_creates_local_dir_if_missing(tmp_path: Path):
    target = tmp_path / "does" / "not" / "exist"
    store = ImageStore(local_dir=target, public_url_prefix="/uploads/flux")

    store.save(b"\x89PNG\r\n\x1a\n")

    assert target.is_dir()


def test_save_rejects_non_png(tmp_path: Path):
    store = ImageStore(local_dir=tmp_path, public_url_prefix="/uploads/flux")

    with pytest.raises(ValueError, match="PNG"):
        store.save(b"not a png")


def test_filenames_are_unique(tmp_path: Path):
    store = ImageStore(local_dir=tmp_path, public_url_prefix="/uploads/flux")
    png = b"\x89PNG\r\n\x1a\n"

    urls = {store.save(png) for _ in range(10)}

    assert len(urls) == 10
```

- [ ] **Step 2.2: Run test — expected to fail (no module)**

```bash
pytest tests/test_image_store.py -v
```

Expected: `ModuleNotFoundError: No module named 'app.image_store'`

- [ ] **Step 2.3: Write minimal implementation**

Create `models/flux2-dev-agent/app/image_store.py`:

```python
"""Persist FLUX PNG output to a shared volume and compute the
public-facing URL.

The local directory is bind-mounted from the host's
``share-dev/uploads/flux/`` into both this container and the
``anila-nginx-dev`` container. Nginx serves it under
``/uploads/flux/`` (sibling of the existing ``/uploads/`` static
route in ``myCSPPlatform/docker/nginx.conf``).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@dataclass(frozen=True)
class ImageStore:
    local_dir: Path
    public_url_prefix: str

    def save(self, png_bytes: bytes) -> str:
        if not png_bytes.startswith(_PNG_MAGIC):
            raise ValueError("payload is not a PNG (magic bytes missing)")

        Path(self.local_dir).mkdir(parents=True, exist_ok=True)

        filename = f"{uuid.uuid4().hex}.png"
        (Path(self.local_dir) / filename).write_bytes(png_bytes)

        return f"{self.public_url_prefix.rstrip('/')}/{filename}"
```

- [ ] **Step 2.4: Run tests, verify all pass**

```bash
pytest tests/test_image_store.py -v
```

Expected: 4 passed.

- [ ] **Step 2.5: Commit**

```bash
git add models/flux2-dev-agent/app/image_store.py models/flux2-dev-agent/tests/test_image_store.py
git commit -m "feat(flux2-dev-agent): image_store for PNG persistence + URL generation"
```

---

## Task 3: flux_client(HTTP client 打 flux2-dev backend)

**Files:**
- Create: `models/flux2-dev-agent/app/flux_client.py`
- Test: `models/flux2-dev-agent/tests/test_flux_client.py`

flux2-dev backend 預期暴露 `POST /generate { prompt: str, aspect_ratio: str }` → 回傳 PNG bytes(`Content-Type: image/png`)。client 把細節包起來,加上 timeout 與簡單的錯誤處理。

- [ ] **Step 3.1: Write the failing test**

Create `models/flux2-dev-agent/tests/test_flux_client.py`:

```python
"""flux_client: HTTP client that calls flux2-dev /generate."""
from __future__ import annotations

import httpx
import pytest
import respx

from app.flux_client import FluxBackendError, FluxClient


_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


@pytest.mark.asyncio
@respx.mock
async def test_generate_returns_png_bytes():
    respx.post("http://flux2-dev:8000/generate").mock(
        return_value=httpx.Response(200, content=_PNG, headers={"content-type": "image/png"})
    )

    async with FluxClient(base_url="http://flux2-dev:8000", timeout=10.0) as client:
        result = await client.generate("a tank in the mountains", aspect_ratio="16:9")

    assert result == _PNG


@pytest.mark.asyncio
@respx.mock
async def test_generate_sends_correct_body():
    route = respx.post("http://flux2-dev:8000/generate").mock(
        return_value=httpx.Response(200, content=_PNG, headers={"content-type": "image/png"})
    )

    async with FluxClient(base_url="http://flux2-dev:8000", timeout=10.0) as client:
        await client.generate("hello", aspect_ratio="1:1")

    assert route.called
    body = route.calls.last.request.content
    import json

    parsed = json.loads(body)
    assert parsed == {"prompt": "hello", "aspect_ratio": "1:1"}


@pytest.mark.asyncio
@respx.mock
async def test_generate_raises_on_non_200():
    respx.post("http://flux2-dev:8000/generate").mock(
        return_value=httpx.Response(500, json={"detail": "OOM"})
    )

    async with FluxClient(base_url="http://flux2-dev:8000", timeout=10.0) as client:
        with pytest.raises(FluxBackendError, match="500"):
            await client.generate("anything", aspect_ratio="16:9")


@pytest.mark.asyncio
@respx.mock
async def test_generate_raises_on_wrong_content_type():
    respx.post("http://flux2-dev:8000/generate").mock(
        return_value=httpx.Response(200, content=b"not a png", headers={"content-type": "text/plain"})
    )

    async with FluxClient(base_url="http://flux2-dev:8000", timeout=10.0) as client:
        with pytest.raises(FluxBackendError, match="content-type"):
            await client.generate("x", aspect_ratio="1:1")
```

- [ ] **Step 3.2: Run test — expected to fail**

```bash
pytest tests/test_flux_client.py -v
```

Expected: `ModuleNotFoundError: No module named 'app.flux_client'`

- [ ] **Step 3.3: Write minimal implementation**

Create `models/flux2-dev-agent/app/flux_client.py`:

```python
"""HTTP client for the flux2-dev inference backend.

The backend is internal to the ``anila-models-net`` docker network
and reachable as ``http://flux2-dev:8000``. It accepts a JSON body
``{prompt, aspect_ratio}`` and returns ``image/png`` bytes.
"""
from __future__ import annotations

from typing import Optional

import httpx


class FluxBackendError(RuntimeError):
    pass


class FluxClient:
    def __init__(self, base_url: str, timeout: float = 120.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "FluxClient":
        self._client = httpx.AsyncClient(timeout=self._timeout)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def generate(self, prompt: str, aspect_ratio: str) -> bytes:
        if self._client is None:
            raise RuntimeError("FluxClient must be used as an async context manager")

        resp = await self._client.post(
            f"{self._base_url}/generate",
            json={"prompt": prompt, "aspect_ratio": aspect_ratio},
        )
        if resp.status_code != 200:
            raise FluxBackendError(f"flux backend returned {resp.status_code}: {resp.text[:200]}")

        ctype = resp.headers.get("content-type", "")
        if not ctype.startswith("image/png"):
            raise FluxBackendError(f"unexpected content-type from flux backend: {ctype!r}")

        return resp.content
```

- [ ] **Step 3.4: Run tests, verify all pass**

```bash
pytest tests/test_flux_client.py -v
```

Expected: 4 passed.

- [ ] **Step 3.5: Commit**

```bash
git add models/flux2-dev-agent/app/flux_client.py models/flux2-dev-agent/tests/test_flux_client.py
git commit -m "feat(flux2-dev-agent): async HTTP client for flux2-dev backend"
```

---

## Task 4: prompt_translator(中文 → FLUX 英文,透過 gemma4 callback)

**Files:**
- Create: `models/flux2-dev-agent/app/prompt_translator.py`
- Test: `models/flux2-dev-agent/tests/test_prompt_translator.py`

呼叫 CSP 的 `/v1/chat/completions`(model=`gemma4`)把使用者口語中文轉成 FLUX 喜歡的英文 prompt。可由環境變數 disable 改成 passthrough。

- [ ] **Step 4.1: Write the failing test**

Create `models/flux2-dev-agent/tests/test_prompt_translator.py`:

```python
"""prompt_translator: rewrite Chinese natural language into a
FLUX-friendly English prompt by calling gemma4 via CSP proxy.

Can be disabled via constructor flag (then it's a no-op pass-through).
"""
from __future__ import annotations

import httpx
import pytest
import respx

from app.prompt_translator import PromptTranslator


@pytest.mark.asyncio
async def test_translator_passthrough_when_disabled():
    translator = PromptTranslator(
        csp_base_url="http://csp:8000",
        csp_api_key="sk-test",
        gemma_model="gemma4",
        enabled=False,
    )
    out = await translator.translate("畫一張坦克")
    assert out == "畫一張坦克"


@pytest.mark.asyncio
@respx.mock
async def test_translator_calls_gemma_and_extracts_english():
    respx.post("http://csp:8000/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "A military tank in mountainous terrain, cinematic, photorealistic",
                        }
                    }
                ]
            },
        )
    )

    translator = PromptTranslator(
        csp_base_url="http://csp:8000",
        csp_api_key="sk-test",
        gemma_model="gemma4",
        enabled=True,
    )
    out = await translator.translate("畫一張在山上的坦克")

    assert "tank" in out.lower()


@pytest.mark.asyncio
@respx.mock
async def test_translator_sends_authorization_header():
    route = respx.post("http://csp:8000/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "x"}}]},
        )
    )

    translator = PromptTranslator(
        csp_base_url="http://csp:8000",
        csp_api_key="sk-secret-key",
        gemma_model="gemma4",
        enabled=True,
    )
    await translator.translate("hi")

    assert route.called
    assert route.calls.last.request.headers["authorization"] == "Bearer sk-secret-key"


@pytest.mark.asyncio
@respx.mock
async def test_translator_falls_back_to_original_on_gemma_error():
    respx.post("http://csp:8000/v1/chat/completions").mock(
        return_value=httpx.Response(500, json={"detail": "vllm down"})
    )

    translator = PromptTranslator(
        csp_base_url="http://csp:8000",
        csp_api_key="sk-test",
        gemma_model="gemma4",
        enabled=True,
    )
    out = await translator.translate("原始輸入")

    # Translation failed but we still got a usable string back.
    assert out == "原始輸入"
```

- [ ] **Step 4.2: Run test — expected to fail**

```bash
pytest tests/test_prompt_translator.py -v
```

Expected: `ModuleNotFoundError: No module named 'app.prompt_translator'`

- [ ] **Step 4.3: Write minimal implementation**

Create `models/flux2-dev-agent/app/prompt_translator.py`:

```python
"""Rewrite the user's natural-language description into a FLUX-friendly
English prompt by delegating to ``gemma4`` through the CSP proxy.

FLUX.2-dev has decent Chinese support (its text encoder is
Mistral-Small-24B which is multilingual), but conversational Chinese
("我想看那個...部隊在山上那種感覺") still benefits from being
rewritten into the descriptive English style FLUX is mostly trained
on. When ``enabled=False`` (e.g. for offline tests or as a kill
switch), this class is a pure pass-through.

On any error from gemma4 we fall back to the original text rather
than failing the whole request — partial degradation is better than
no image at all.
"""
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You rewrite a user's casual image-generation request into a single "
    "concise English prompt suitable for the FLUX.2-dev text-to-image "
    "model. Preserve all concrete subjects, settings, props, and mood. "
    "Add brief style hints (composition, lighting, photographic vs. "
    "illustrated) only when they are clearly implied. Reply with ONLY "
    "the rewritten prompt — no quotes, no commentary, no leading 'Prompt:'."
)


class PromptTranslator:
    def __init__(
        self,
        *,
        csp_base_url: str,
        csp_api_key: str,
        gemma_model: str,
        enabled: bool,
        timeout: float = 15.0,
    ) -> None:
        self._csp_base_url = csp_base_url.rstrip("/")
        self._csp_api_key = csp_api_key
        self._gemma_model = gemma_model
        self._enabled = enabled
        self._timeout = timeout

    async def translate(self, user_text: str) -> str:
        if not self._enabled:
            return user_text

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._csp_base_url}/v1/chat/completions",
                    headers={"Authorization": f"Bearer {self._csp_api_key}"},
                    json={
                        "model": self._gemma_model,
                        "messages": [
                            {"role": "system", "content": _SYSTEM_PROMPT},
                            {"role": "user", "content": user_text},
                        ],
                        "stream": False,
                        "temperature": 0.2,
                    },
                )
            if resp.status_code != 200:
                logger.warning(
                    "prompt translation failed (status=%s); falling back to original",
                    resp.status_code,
                )
                return user_text

            data = resp.json()
            content = data["choices"][0]["message"]["content"].strip()
            return content or user_text
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
            logger.warning("prompt translation errored (%s); falling back to original", exc)
            return user_text
```

- [ ] **Step 4.4: Run tests, verify all pass**

```bash
pytest tests/test_prompt_translator.py -v
```

Expected: 4 passed.

- [ ] **Step 4.5: Commit**

```bash
git add models/flux2-dev-agent/app/prompt_translator.py models/flux2-dev-agent/tests/test_prompt_translator.py
git commit -m "feat(flux2-dev-agent): gemma4-backed prompt translator with safe fallback"
```

---

## Task 5: chat_handler(把上述三個元件組合成完整 chat completion 流程)

**Files:**
- Create: `models/flux2-dev-agent/app/chat_handler.py`
- Test: `models/flux2-dev-agent/tests/test_chat_handler.py`

整個 `/v1/chat/completions` 的處理邏輯放這裡:接 ChatCompletionRequest → 抽 last user text → 翻譯 prompt → 呼叫 FLUX → 落地 PNG → 組 OpenAI shape response。FastAPI 層只負責 binding,測試友善。

- [ ] **Step 5.1: Write the failing test**

Create `models/flux2-dev-agent/tests/test_chat_handler.py`:

```python
"""chat_handler: orchestrate translator + flux_client + image_store
and return an OpenAI-shape ChatCompletionResponse whose assistant
message contains a markdown image tag.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.chat_handler import ChatHandler
from app.image_store import ImageStore
from app.schemas import ChatCompletionRequest, ChatMessage


_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


@pytest.fixture
def store(tmp_path: Path) -> ImageStore:
    return ImageStore(local_dir=tmp_path, public_url_prefix="/uploads/flux")


@pytest.mark.asyncio
async def test_handle_returns_markdown_image_in_assistant_content(store: ImageStore):
    translator = AsyncMock()
    translator.translate.return_value = "a tank in the mountains"

    flux_client = AsyncMock()
    flux_client.generate.return_value = _PNG

    handler = ChatHandler(
        translator=translator,
        flux_client_factory=lambda: _AsyncContext(flux_client),
        image_store=store,
        default_aspect_ratio="16:9",
    )

    req = ChatCompletionRequest(
        model="image-generator",
        messages=[ChatMessage(role="user", content="畫一張在山上的坦克")],
    )
    resp = await handler.handle(req)

    assert resp.model == "image-generator"
    assert len(resp.choices) == 1
    content = resp.choices[0].message.content
    assert content.startswith("已為您繪製")
    assert "![](" in content
    assert "/uploads/flux/" in content
    assert content.rstrip().endswith(".png)")


@pytest.mark.asyncio
async def test_handle_translates_prompt_before_calling_flux(store: ImageStore):
    translator = AsyncMock()
    translator.translate.return_value = "ENGLISH PROMPT"

    flux_client = AsyncMock()
    flux_client.generate.return_value = _PNG

    handler = ChatHandler(
        translator=translator,
        flux_client_factory=lambda: _AsyncContext(flux_client),
        image_store=store,
        default_aspect_ratio="16:9",
    )

    req = ChatCompletionRequest(
        model="image-generator",
        messages=[ChatMessage(role="user", content="原始中文")],
    )
    await handler.handle(req)

    translator.translate.assert_awaited_once_with("原始中文")
    flux_client.generate.assert_awaited_once()
    call_args = flux_client.generate.await_args
    assert call_args.args[0] == "ENGLISH PROMPT" or call_args.kwargs.get("prompt") == "ENGLISH PROMPT"


@pytest.mark.asyncio
async def test_handle_passes_default_aspect_ratio(store: ImageStore):
    translator = AsyncMock()
    translator.translate.return_value = "x"
    flux_client = AsyncMock()
    flux_client.generate.return_value = _PNG

    handler = ChatHandler(
        translator=translator,
        flux_client_factory=lambda: _AsyncContext(flux_client),
        image_store=store,
        default_aspect_ratio="1:1",
    )

    req = ChatCompletionRequest(
        model="image-generator",
        messages=[ChatMessage(role="user", content="x")],
    )
    await handler.handle(req)

    call = flux_client.generate.await_args
    aspect = call.kwargs.get("aspect_ratio") or (call.args[1] if len(call.args) > 1 else None)
    assert aspect == "1:1"


class _AsyncContext:
    """Minimal async-context-manager wrapper around an already-built mock."""

    def __init__(self, target):
        self._target = target

    async def __aenter__(self):
        return self._target

    async def __aexit__(self, *exc):
        return None
```

- [ ] **Step 5.2: Run test — expected to fail**

```bash
pytest tests/test_chat_handler.py -v
```

Expected: `ModuleNotFoundError: No module named 'app.chat_handler'`

- [ ] **Step 5.3: Write minimal implementation**

Create `models/flux2-dev-agent/app/chat_handler.py`:

```python
"""Coordinate translator + flux_client + image_store and assemble an
OpenAI-shape ChatCompletionResponse.

``flux_client_factory`` returns a context manager that yields the
actual ``FluxClient`` — this lets the handler own connection lifetime
per request without hard-coding the constructor (so tests can inject
a pre-built mock).
"""
from __future__ import annotations

import time
import uuid
from typing import Awaitable, Callable, Protocol

from .image_store import ImageStore
from .schemas import (
    ChatCompletionChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
)


class _TranslatorProto(Protocol):
    async def translate(self, user_text: str) -> str: ...


class _FluxClientProto(Protocol):
    async def generate(self, prompt: str, aspect_ratio: str) -> bytes: ...


class _FluxClientCtxProto(Protocol):
    async def __aenter__(self) -> _FluxClientProto: ...
    async def __aexit__(self, *exc) -> None: ...


class ChatHandler:
    def __init__(
        self,
        *,
        translator: _TranslatorProto,
        flux_client_factory: Callable[[], _FluxClientCtxProto],
        image_store: ImageStore,
        default_aspect_ratio: str = "16:9",
    ) -> None:
        self._translator = translator
        self._flux_client_factory = flux_client_factory
        self._image_store = image_store
        self._default_aspect_ratio = default_aspect_ratio

    async def handle(self, req: ChatCompletionRequest) -> ChatCompletionResponse:
        user_text = req.last_user_text()
        english_prompt = await self._translator.translate(user_text)

        async with self._flux_client_factory() as flux:
            png_bytes = await flux.generate(
                prompt=english_prompt,
                aspect_ratio=self._default_aspect_ratio,
            )

        url = self._image_store.save(png_bytes)

        content = f"已為您繪製：\n\n![]({url})"

        return ChatCompletionResponse(
            id=f"flux-{uuid.uuid4().hex}",
            created=int(time.time()),
            model=req.model,
            choices=[
                ChatCompletionChoice(
                    index=0,
                    message=ChatMessage(role="assistant", content=content),
                    finish_reason="stop",
                ),
            ],
        )
```

- [ ] **Step 5.4: Run tests, verify all pass**

```bash
pytest tests/test_chat_handler.py -v
```

Expected: 3 passed.

- [ ] **Step 5.5: Commit**

```bash
git add models/flux2-dev-agent/app/chat_handler.py models/flux2-dev-agent/tests/test_chat_handler.py
git commit -m "feat(flux2-dev-agent): chat handler that returns markdown image"
```

---

## Task 6: FastAPI main entrypoint + health/chat endpoints

**Files:**
- Create: `models/flux2-dev-agent/app/main.py`
- Test: `models/flux2-dev-agent/tests/test_main.py`

把上面元件接到 FastAPI 上,加 `/health` 與 `/v1/chat/completions`。讀 env 構建依賴。

- [ ] **Step 6.1: Write the failing test**

Create `models/flux2-dev-agent/tests/test_main.py`:

```python
"""FastAPI app integration test — exercises /health and
/v1/chat/completions with all collaborators stubbed.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.image_store import ImageStore
from app.main import build_app


_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    translator = AsyncMock()
    translator.translate.return_value = "english prompt"

    flux_client = AsyncMock()
    flux_client.generate.return_value = _PNG

    class _Ctx:
        async def __aenter__(self):
            return flux_client

        async def __aexit__(self, *exc):
            return None

    app = build_app(
        translator=translator,
        flux_client_factory=lambda: _Ctx(),
        image_store=ImageStore(local_dir=tmp_path, public_url_prefix="/uploads/flux"),
        default_aspect_ratio="16:9",
    )
    return TestClient(app)


def test_health_returns_200(client: TestClient):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_chat_completions_returns_openai_shape(client: TestClient):
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "image-generator",
            "messages": [{"role": "user", "content": "畫一張坦克"}],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["role"] == "assistant"
    assert "![](" in body["choices"][0]["message"]["content"]


def test_chat_completions_rejects_empty_messages(client: TestClient):
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "image-generator", "messages": []},
    )
    assert resp.status_code == 422


def test_models_endpoint_lists_image_generator(client: TestClient):
    resp = client.get("/v1/models")
    assert resp.status_code == 200
    body = resp.json()
    ids = [m["id"] for m in body["data"]]
    assert "image-generator" in ids
```

- [ ] **Step 6.2: Run test — expected to fail**

```bash
pytest tests/test_main.py -v
```

Expected: import error or attribute error.

- [ ] **Step 6.3: Write minimal implementation**

Create `models/flux2-dev-agent/app/main.py`:

```python
"""FastAPI entrypoint for flux2-dev-agent.

Wires runtime config (env vars) to the four collaborators
(translator, flux client factory, image store, chat handler) and
exposes ``/health``, ``/v1/models``, ``/v1/chat/completions``.

``build_app`` takes the collaborators as parameters so tests can
inject mocks; the module-level ``app`` instance built from env is
what uvicorn imports.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException

from .chat_handler import ChatHandler
from .flux_client import FluxClient
from .image_store import ImageStore
from .prompt_translator import PromptTranslator
from .schemas import ChatCompletionRequest, ChatCompletionResponse

logger = logging.getLogger(__name__)


def build_app(
    *,
    translator,
    flux_client_factory: Callable,
    image_store: ImageStore,
    default_aspect_ratio: str,
) -> FastAPI:
    app = FastAPI(title="flux2-dev-agent", version="0.1.0")
    handler = ChatHandler(
        translator=translator,
        flux_client_factory=flux_client_factory,
        image_store=image_store,
        default_aspect_ratio=default_aspect_ratio,
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/models")
    def list_models() -> dict:
        return {
            "object": "list",
            "data": [
                {"id": "image-generator", "object": "model", "owned_by": "anila"},
            ],
        }

    @app.post("/v1/chat/completions", response_model=ChatCompletionResponse)
    async def chat_completions(req: ChatCompletionRequest) -> ChatCompletionResponse:
        try:
            return await handler.handle(req)
        except Exception as exc:
            logger.exception("flux generation failed")
            raise HTTPException(status_code=502, detail=f"image generation failed: {exc}")

    return app


def _build_from_env() -> FastAPI:
    flux_backend_url = os.environ.get("FLUX_BACKEND_URL", "http://flux2-dev:8000")
    csp_base_url = os.environ.get("CSP_BASE_URL", "http://csp:8000")
    csp_api_key = os.environ.get("CSP_API_KEY", "")
    gemma_model = os.environ.get("GEMMA_MODEL", "gemma4")
    enable_translation = os.environ.get("ENABLE_PROMPT_TRANSLATION", "1") == "1"
    share_dir = Path(os.environ.get("SHARE_DIR", "/share/flux"))
    public_prefix = os.environ.get("PUBLIC_URL_PREFIX", "/uploads/flux")
    aspect_ratio = os.environ.get("DEFAULT_ASPECT_RATIO", "16:9")
    flux_timeout = float(os.environ.get("FLUX_TIMEOUT_SECONDS", "180"))

    translator = PromptTranslator(
        csp_base_url=csp_base_url,
        csp_api_key=csp_api_key,
        gemma_model=gemma_model,
        enabled=enable_translation and bool(csp_api_key),
    )

    def flux_factory():
        return FluxClient(base_url=flux_backend_url, timeout=flux_timeout)

    store = ImageStore(local_dir=share_dir, public_url_prefix=public_prefix)

    return build_app(
        translator=translator,
        flux_client_factory=flux_factory,
        image_store=store,
        default_aspect_ratio=aspect_ratio,
    )


app = _build_from_env()
```

- [ ] **Step 6.4: Run tests, verify all pass**

```bash
pytest tests/test_main.py -v
```

Expected: 4 passed.

- [ ] **Step 6.5: Run all flux2-dev-agent tests together to confirm green**

```bash
pytest tests/ -v
```

Expected: 23 passed (4 schemas + 4 image_store + 4 flux_client + 4 translator + 3 chat_handler + 4 main).

- [ ] **Step 6.6: Commit**

```bash
git add models/flux2-dev-agent/app/main.py models/flux2-dev-agent/tests/test_main.py
git commit -m "feat(flux2-dev-agent): FastAPI entrypoint with health/models/chat endpoints"
```

---

## Task 7: Dockerfile for flux2-dev-agent

**Files:**
- Create: `models/flux2-dev-agent/Dockerfile`
- Create: `models/flux2-dev-agent/.dockerignore`

- [ ] **Step 7.1: Write Dockerfile**

Create `models/flux2-dev-agent/Dockerfile`:

```dockerfile
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# /share/flux is bind-mounted from the host at runtime; pre-create it
# so the bind mount has a target that ``image_store`` will mkdir into
# (mkdir(parents=True, exist_ok=True) handles the rest at runtime).
RUN mkdir -p /share/flux

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **Step 7.2: Write .dockerignore**

Create `models/flux2-dev-agent/.dockerignore`:

```
.venv/
__pycache__/
*.pyc
tests/
.pytest_cache/
pyproject.toml
```

- [ ] **Step 7.3: Build the image locally to verify it works**

```bash
cd /home/aia/c1147259/ANILA/models/flux2-dev-agent
docker build -t anila-flux-agent:dev .
```

Expected: build completes with no errors.

- [ ] **Step 7.4: Smoke-test the built image without backend**

```bash
docker run --rm -d --name flux-agent-smoke -p 18099:8000 \
    -e FLUX_BACKEND_URL=http://localhost:1 \
    -e CSP_BASE_URL=http://localhost:1 \
    -e ENABLE_PROMPT_TRANSLATION=0 \
    anila-flux-agent:dev
sleep 3
curl -sf http://localhost:18099/health
curl -sf http://localhost:18099/v1/models
docker stop flux-agent-smoke
```

Expected: `{"status":"ok"}` and the models list including `image-generator`.

- [ ] **Step 7.5: Commit**

```bash
git add models/flux2-dev-agent/Dockerfile models/flux2-dev-agent/.dockerignore
git commit -m "build(flux2-dev-agent): Dockerfile for the shim container"
```

---

## Task 8: flux2-dev inference server skeleton + tests (mocked pipeline)

**Files:**
- Create: `models/flux2-dev/server.py`
- Create: `models/flux2-dev/requirements.txt`
- Create: `models/flux2-dev/pyproject.toml`
- Create: `models/flux2-dev/tests/__init__.py`
- Test: `models/flux2-dev/tests/test_server.py`

不要在測試裡載真實 Flux2Pipeline(會把 80GB 權重塞進記憶體)。把 pipeline 注入式設計,測試用 mock。

- [ ] **Step 8.1: Write requirements.txt**

Create `models/flux2-dev/requirements.txt`:

```
fastapi==0.115.5
uvicorn[standard]==0.32.1
pydantic==2.9.2
# Inference deps (NOT pinned exact because we layer on top of a CUDA base image)
torch==2.4.0
diffusers>=0.32.0
transformers>=4.46.0
accelerate>=1.1.0
sentencepiece
protobuf
safetensors
Pillow
```

- [ ] **Step 8.2: Write pyproject.toml(只給 tests 用)**

Create `models/flux2-dev/pyproject.toml`:

```toml
[project]
name = "flux2-dev"
version = "0.1.0"
requires-python = ">=3.11"
# Note: torch/diffusers are NOT in test deps — tests inject a mocked pipeline
# so we never have to install the GPU stack on the dev box.
dependencies = [
    "fastapi==0.115.5",
    "pydantic==2.9.2",
    "Pillow",
]

[project.optional-dependencies]
test = [
    "pytest==8.3.3",
    "httpx==0.27.2",  # for fastapi TestClient
]

[tool.pytest.ini_options]
pythonpath = ["."]
```

- [ ] **Step 8.3: Write failing test**

Create `models/flux2-dev/tests/test_server.py`:

```python
"""Server endpoint shape — uses an injected mock pipeline so we never
load real 80GB weights in CI/dev.
"""
from __future__ import annotations

from io import BytesIO

from fastapi.testclient import TestClient
from PIL import Image

from server import build_app


class _MockPipeline:
    """Stand-in for the real diffusers Flux2Pipeline."""

    def __init__(self, captured: list[dict]):
        self._captured = captured

    def __call__(self, **kwargs):
        self._captured.append(kwargs)
        img = Image.new("RGB", (kwargs["width"], kwargs["height"]), color=(10, 20, 30))

        class _Out:
            images = [img]

        return _Out()


def _make_client() -> tuple[TestClient, list[dict]]:
    captured: list[dict] = []
    app = build_app(pipeline=_MockPipeline(captured))
    return TestClient(app), captured


def test_health_ok():
    client, _ = _make_client()
    r = client.get("/health")
    assert r.status_code == 200


def test_generate_returns_png():
    client, captured = _make_client()
    r = client.post("/generate", json={"prompt": "test", "aspect_ratio": "1:1"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/png")
    img = Image.open(BytesIO(r.content))
    assert img.format == "PNG"


def test_generate_passes_prompt_to_pipeline():
    client, captured = _make_client()
    client.post("/generate", json={"prompt": "a tank", "aspect_ratio": "16:9"})
    assert len(captured) == 1
    assert captured[0]["prompt"] == "a tank"


def test_generate_translates_aspect_ratio_to_dimensions():
    client, captured = _make_client()

    client.post("/generate", json={"prompt": "x", "aspect_ratio": "1:1"})
    client.post("/generate", json={"prompt": "x", "aspect_ratio": "16:9"})
    client.post("/generate", json={"prompt": "x", "aspect_ratio": "9:16"})

    sq, wide, tall = captured
    assert sq["width"] == sq["height"]
    assert wide["width"] > wide["height"]
    assert tall["height"] > tall["width"]


def test_generate_rejects_unknown_aspect_ratio():
    client, _ = _make_client()
    r = client.post("/generate", json={"prompt": "x", "aspect_ratio": "47:11"})
    assert r.status_code == 422
```

- [ ] **Step 8.4: Run tests (will fail — server.py not written yet)**

```bash
cd /home/aia/c1147259/ANILA/models/flux2-dev
python -m venv .venv && source .venv/bin/activate
pip install -e ".[test]"
pytest tests/test_server.py -v
```

Expected: `ModuleNotFoundError: No module named 'server'`

- [ ] **Step 8.5: Write minimal server.py**

Create `models/flux2-dev/server.py`:

```python
"""flux2-dev: minimal HTTP wrapper around diffusers Flux2Pipeline.

Endpoints:
  GET  /health                                  → {"status": "ok"}
  POST /generate {prompt, aspect_ratio}         → image/png bytes

The pipeline is constructed once at module import and injected into
``build_app``. Tests pass a mock pipeline so we never load the real
weights outside of production runs.
"""
from __future__ import annotations

import io
import logging
import os
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

logger = logging.getLogger(__name__)

_ASPECT_RATIOS: dict[str, tuple[int, int]] = {
    "1:1": (1024, 1024),
    "16:9": (1408, 768),
    "9:16": (768, 1408),
    "4:3": (1216, 896),
    "3:4": (896, 1216),
}


class GenerateRequest(BaseModel):
    prompt: str
    aspect_ratio: Literal["1:1", "16:9", "9:16", "4:3", "3:4"] = "16:9"


def build_app(*, pipeline: Any) -> FastAPI:
    app = FastAPI(title="flux2-dev", version="0.1.0")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/generate")
    def generate(req: GenerateRequest) -> Response:
        width, height = _ASPECT_RATIOS[req.aspect_ratio]
        try:
            out = pipeline(
                prompt=req.prompt,
                width=width,
                height=height,
                num_inference_steps=int(os.environ.get("FLUX_NUM_STEPS", "28")),
                guidance_scale=float(os.environ.get("FLUX_GUIDANCE_SCALE", "3.5")),
            )
        except Exception as exc:
            logger.exception("flux inference failed")
            raise HTTPException(status_code=500, detail=f"inference failed: {exc}")

        img = out.images[0]
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return Response(content=buf.getvalue(), media_type="image/png")

    return app


def _load_pipeline_from_env():
    """Load the real Flux2Pipeline. Called once at process start.

    Kept out of ``build_app`` so tests don't need GPU.
    """
    import torch  # type: ignore
    from diffusers import Flux2Pipeline  # type: ignore

    model_path = os.environ.get("FLUX_MODEL_PATH", "/workspace/model/FLUX.2-dev")
    device_map = os.environ.get("FLUX_DEVICE_MAP", "balanced")

    logger.info("loading FLUX.2-dev from %s (device_map=%s)", model_path, device_map)
    pipe = Flux2Pipeline.from_pretrained(
        model_path,
        torch_dtype=torch.bfloat16,
        device_map=device_map,
    )
    return pipe


def _build_for_runtime() -> FastAPI:
    if os.environ.get("FLUX_SKIP_LOAD") == "1":
        # Smoke-test path: build with a no-op stub so the container
        # comes up healthy without GPUs (used in integration tests).
        class _Stub:
            def __call__(self, **kwargs):
                from PIL import Image

                img = Image.new("RGB", (kwargs["width"], kwargs["height"]), color=(0, 0, 0))

                class _Out:
                    images = [img]

                return _Out()

        return build_app(pipeline=_Stub())
    return build_app(pipeline=_load_pipeline_from_env())


app = _build_for_runtime()
```

- [ ] **Step 8.6: Run tests**

```bash
pytest tests/test_server.py -v
```

Expected: 5 passed.

- [ ] **Step 8.7: Commit**

```bash
git add models/flux2-dev/
git commit -m "feat(flux2-dev): FastAPI inference server with injectable pipeline"
```

---

## Task 9: Dockerfile for flux2-dev

**Files:**
- Create: `models/flux2-dev/Dockerfile`
- Create: `models/flux2-dev/.dockerignore`

CUDA 12.4 + cuDNN base image,裝 diffusers + transformers + torch。**不下載模型**(模型 bind-mount 進來,沿用既有 HF 慣例)。

- [ ] **Step 9.1: Write Dockerfile**

Create `models/flux2-dev/Dockerfile`:

```dockerfile
# Match the CUDA major.minor of the host driver (H100 typically 12.x).
# pytorch 2.4 wheels target cu124.
FROM nvidia/cuda:12.4.1-cudnn-devel-ubuntu22.04

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.11 python3-pip python3.11-venv \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN ln -sf /usr/bin/python3.11 /usr/bin/python && \
    ln -sf /usr/bin/pip3 /usr/bin/pip

WORKDIR /workspace

COPY requirements.txt .
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cu124 torch==2.4.0 && \
    pip install --no-cache-dir -r requirements.txt

COPY server.py .

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=8 \
    CMD curl -sf http://localhost:8000/health || exit 1

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **Step 9.2: Write .dockerignore**

Create `models/flux2-dev/.dockerignore`:

```
.venv/
__pycache__/
*.pyc
tests/
.pytest_cache/
pyproject.toml
```

- [ ] **Step 9.3: Build (this will take ~10-15 min due to torch + CUDA)**

```bash
cd /home/aia/c1147259/ANILA/models/flux2-dev
docker build -t flux2-dev:bf16 .
```

Expected: build completes; image is ~12-15 GB due to CUDA + torch.

- [ ] **Step 9.4: Smoke test with FLUX_SKIP_LOAD=1 (no GPU, no model)**

```bash
docker run --rm -d --name flux-smoke -p 18100:8000 \
    -e FLUX_SKIP_LOAD=1 \
    flux2-dev:bf16
sleep 5
curl -sf http://localhost:18100/health
curl -sf -X POST http://localhost:18100/generate \
    -H "Content-Type: application/json" \
    -d '{"prompt":"smoke","aspect_ratio":"1:1"}' \
    -o /tmp/smoke.png
file /tmp/smoke.png
docker stop flux-smoke
```

Expected: `/tmp/smoke.png` is identified as a PNG by `file`.

- [ ] **Step 9.5: Commit**

```bash
git add models/flux2-dev/Dockerfile models/flux2-dev/.dockerignore
git commit -m "build(flux2-dev): CUDA 12.4 Dockerfile with diffusers"
```

---

## Task 10: Add both services to `models/docker-compose.yml`

**Files:**
- Modify: `models/docker-compose.yml`

加在 `nv-embed-proxy` 那段下面,`networks:` 區塊上面。

- [ ] **Step 10.1: Open the file and locate the insertion point**

開 `models/docker-compose.yml`,找到第 217 行 `# ── Embedding FastAPI shim` 段結束、`networks:` 區塊開始之前(目前該檔約 222 行)。

- [ ] **Step 10.2: Insert the two new service blocks**

在 `networks:` 區塊**之前**插入:

```yaml
  # ── Image generation: FLUX.2-dev on GPU 1 + GPU 2 ─────────────────────────
  # bf16 dual-H100 via diffusers Flux2Pipeline (device_map="balanced").
  # No host port — agent shim is the only client. Soldier-facing UX
  # never sees this service directly; chat 走 router → DISPATCH:image-generator
  # → CSP proxy → flux2-dev-agent → flux2-dev.
  #
  # License caveat: black-forest-labs/FLUX.2-dev is BFL Non-Commercial.
  # Confirm authorization before enabling in production.
  flux2-dev:
    image: flux2-dev:bf16
    container_name: anila-model-flux2-dev
    build:
      context: ./flux2-dev
    environment:
      TZ: Asia/Taipei
      HF_HUB_OFFLINE: "1"
      TRANSFORMERS_OFFLINE: "1"
      FLUX_MODEL_PATH: /workspace/model/FLUX.2-dev
      FLUX_DEVICE_MAP: balanced
      FLUX_NUM_STEPS: "28"
      FLUX_GUIDANCE_SCALE: "3.5"
    volumes:
      - /home/aia/c1147259/project/Huggingface/FLUX.2-dev:/workspace/model/FLUX.2-dev:ro
    expose:
      - "8000"
    restart: unless-stopped
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              device_ids: ["1", "2"]
              capabilities: [gpu]
    shm_size: 32g
    ipc: host
    ulimits:
      memlock: -1
      stack: 67108864
    networks: [models]
    healthcheck:
      test: ["CMD-SHELL", "curl -sf http://localhost:8000/health > /dev/null"]
      interval: 30s
      timeout: 10s
      retries: 10
      start_period: 300s   # FLUX.2-dev 載入權重 + warmup 約 3-5 分鐘

  # ── Image generation agent shim (OpenAI compat front for flux2-dev) ───────
  # CSP registers THIS as the agent endpoint (model_type=agent). When
  # gemma4 emits DISPATCH:image-generator:..., CSP forwards the request
  # here. Shim handles prompt translation (zh→en via gemma4 callback)
  # and image persistence; binds /share/flux from share-dev/uploads/flux.
  flux2-dev-agent:
    image: anila-flux-agent:latest
    container_name: anila-model-flux2-dev-agent
    build:
      context: ./flux2-dev-agent
    environment:
      TZ: Asia/Taipei
      FLUX_BACKEND_URL: http://flux2-dev:8000
      CSP_BASE_URL: http://csp:8000
      CSP_API_KEY: ${INTERNAL_PLATFORM_API_KEY_DEV:-sk-internal-worker-dev-changeme}
      GEMMA_MODEL: gemma4
      ENABLE_PROMPT_TRANSLATION: "1"
      SHARE_DIR: /share/flux
      PUBLIC_URL_PREFIX: /uploads/flux
      DEFAULT_ASPECT_RATIO: "16:9"
      FLUX_TIMEOUT_SECONDS: "240"
    volumes:
      - /home/aia/c1147259/ANILA/share-dev/uploads/flux:/share/flux
    expose:
      - "8000"
    depends_on:
      flux2-dev:
        condition: service_healthy
    restart: unless-stopped
    networks: [models]
    healthcheck:
      test: ["CMD-SHELL", "curl -sf http://localhost:8000/health > /dev/null"]
      interval: 30s
      timeout: 10s
      retries: 5
      start_period: 20s
```

- [ ] **Step 10.3: Ensure share-dev/uploads/flux directory exists on host**

```bash
mkdir -p /home/aia/c1147259/ANILA/share-dev/uploads/flux
```

- [ ] **Step 10.4: Validate compose syntax**

```bash
cd /home/aia/c1147259/ANILA
docker compose -f models/docker-compose.yml config --quiet
```

Expected: no output (silent success).

- [ ] **Step 10.5: Commit**

```bash
git add models/docker-compose.yml
git commit -m "feat(models): add flux2-dev + flux2-dev-agent to compose"
```

---

## Task 11: Register image-generator agent in CSP `AUTO_REGISTER_MODELS`

**Files:**
- Modify: `docker-compose-dev.yml:68-69`

把現有 JSON array 多塞一個 element。注意:該 JSON 是 YAML `>-` block scalar(一整行),改的時候要小心 escape。

- [ ] **Step 11.1: Read the current AUTO_REGISTER_MODELS line**

確認當前長相(對照 `docker-compose-dev.yml` line 68-69)。當前內容:

```yaml
      AUTO_REGISTER_MODELS: >-
        [{"name":"${LOCAL_LLM_MODEL:-gpt-oss-20b}","display_name":"${LOCAL_LLM_MODEL:-gpt-oss-20b}","model_type":"llm","endpoint_url":"${LOCAL_LLM_BASE_URL:-http://gpt-oss-20b:8000}","api_version":"v1","description":"On-prem local LLM (anila-models-net)"},{"name":"${LOCAL_EMBEDDING_MODEL:-nvidia/NV-embed-V2}","display_name":"${LOCAL_EMBEDDING_MODEL:-nvidia/NV-embed-V2}","model_type":"embedding","endpoint_url":"${LOCAL_EMBEDDING_BASE_URL:-http://nv-embed-proxy:8000}","api_version":"v1","description":"On-prem NV-embed-V2 via FastAPI shim (anila-models-net)"}]
```

- [ ] **Step 11.2: Modify to append the image-generator entry**

Replace line 68-69 with(在最後一個 `}` 之前加一個逗號和新的 agent object):

```yaml
      AUTO_REGISTER_MODELS: >-
        [{"name":"${LOCAL_LLM_MODEL:-gpt-oss-20b}","display_name":"${LOCAL_LLM_MODEL:-gpt-oss-20b}","model_type":"llm","endpoint_url":"${LOCAL_LLM_BASE_URL:-http://gpt-oss-20b:8000}","api_version":"v1","description":"On-prem local LLM (anila-models-net)"},{"name":"${LOCAL_EMBEDDING_MODEL:-nvidia/NV-embed-V2}","display_name":"${LOCAL_EMBEDDING_MODEL:-nvidia/NV-embed-V2}","model_type":"embedding","endpoint_url":"${LOCAL_EMBEDDING_BASE_URL:-http://nv-embed-proxy:8000}","api_version":"v1","description":"On-prem NV-embed-V2 via FastAPI shim (anila-models-net)"},{"name":"image-generator","display_name":"圖像繪製","model_type":"agent","endpoint_url":"http://flux2-dev-agent:8000","api_version":"v1","description":"FLUX.2-dev image generation agent","description_for_router":"當使用者要求繪製、產生、設計、畫一張、給我一張圖、視覺化、想看看 XX 的樣子、想要看一張圖等等的時候使用。輸入是使用者的中文自然語言描述,不需要先翻譯,agent 內部會處理。"}]
```

注意:新增的 element 必須有 `description_for_router`,因為這是 Router LLM 派發判斷的唯一依據。

- [ ] **Step 11.3: Validate compose syntax**

```bash
docker compose -f docker-compose-dev.yml config --quiet
```

Expected: silent success.

- [ ] **Step 11.4: Commit**

```bash
git add docker-compose-dev.yml
git commit -m "feat(csp): auto-register image-generator agent for FLUX dispatch"
```

---

## Task 12: 確認 nginx `/uploads/` 已經會 serve FLUX 圖檔

**Files:**
- Read-only check: `myCSPPlatform/docker/nginx.conf` (lines around 338, 622)

理論上現有 `location /uploads/` 已經 alias 到 `/usr/share/nginx/share-files/uploads/`,而 compose 又把 `./share-dev/uploads` mount 到那個路徑。所以 `share-dev/uploads/flux/foo.png` 自動就可以從 `/uploads/flux/foo.png` 拿到。確認一下。

- [ ] **Step 12.1: Read nginx.conf to confirm /uploads/ aliasing**

```bash
grep -A 4 'location /uploads/' /home/aia/c1147259/ANILA/myCSPPlatform/docker/nginx.conf
```

Expected output(兩個 server block 各一段):

```
location /uploads/ {
    alias /usr/share/nginx/share-files/uploads/;
    ...
}
```

- [ ] **Step 12.2: Confirm volume mount maps to that path**

```bash
grep -B 1 -A 1 'share-files/uploads' /home/aia/c1147259/ANILA/docker-compose-dev.yml
```

Expected: 看到 `./share-dev/uploads:/usr/share/nginx/share-files/uploads:rw`。

- [ ] **Step 12.3: Touch a placeholder file and verify nginx serves it**

```bash
mkdir -p /home/aia/c1147259/ANILA/share-dev/uploads/flux
echo "placeholder" > /home/aia/c1147259/ANILA/share-dev/uploads/flux/.gitkeep
# 找出 dev nginx 暴露的 port (從 docker-compose-dev.yml 看)
docker compose -f docker-compose-dev.yml ps nginx
# 假設 :8080 是 http
curl -sf http://localhost:8080/uploads/flux/.gitkeep
```

Expected: 看到 `placeholder`。如果 404,nginx 設定就要加 `/uploads/flux/` 專屬 location,但通常不會。

- [ ] **Step 12.4(僅在 Step 12.3 失敗時)**:加 nginx location

如果 Step 12.3 失敗(例如現有 `location /uploads/` 有 access control 或 deny 條件擋掉了),在 `myCSPPlatform/docker/nginx.conf` 兩個 server block 各加:

```nginx
location /uploads/flux/ {
    alias /usr/share/nginx/share-files/uploads/flux/;
    expires 7d;
    add_header Cache-Control "public, max-age=604800";
    try_files $uri =404;
}
```

並 reload:

```bash
docker compose -f docker-compose-dev.yml exec nginx nginx -s reload
```

Re-run Step 12.3 to verify.

- [ ] **Step 12.5: Commit(如果有改 nginx.conf)**

```bash
git add myCSPPlatform/docker/nginx.conf share-dev/uploads/flux/.gitkeep
git commit -m "feat(nginx): explicit /uploads/flux/ location for FLUX-generated images"
```

如果沒改任何檔,跳過 commit。

---

## Task 13: End-to-end smoke test

**Files:**
- Read-only

- [ ] **Step 13.1: 確認 FLUX.2-dev 權重已下載到 host**

```bash
ls -la /home/aia/c1147259/project/Huggingface/FLUX.2-dev/
du -sh /home/aia/c1147259/project/Huggingface/FLUX.2-dev/
```

Expected: 看到 `*.safetensors`、`config.json`、`model_index.json` 等,總大小約 110-120 GB。**如果這步失敗就停下**,先跑 `huggingface-cli download` 把模型抓下來(見前置條件 #2)。

- [ ] **Step 13.2: 確認 GPU 1 + GPU 2 真的還是閒置**

```bash
nvidia-smi --query-gpu=index,memory.used,memory.free --format=csv
```

Expected: GPU 1 與 GPU 2 的 `memory.used` 接近 0 MiB。如果有東西在跑,先處理掉再繼續(避免 OOM)。

- [ ] **Step 13.3: Build 兩個 image(如果還沒 build)**

```bash
cd /home/aia/c1147259/ANILA
docker compose -f models/docker-compose.yml build flux2-dev flux2-dev-agent
```

- [ ] **Step 13.4: 啟動 FLUX 服務(只起這兩個,不動現有)**

```bash
docker compose -f models/docker-compose.yml up -d flux2-dev flux2-dev-agent
```

- [ ] **Step 13.5: Tail logs until flux2-dev healthy(會花 3-5 分鐘載 32B 模型)**

```bash
docker compose -f models/docker-compose.yml logs -f flux2-dev
```

等到看到 `Application startup complete` 與 healthcheck 變 `(healthy)`(在另一個 terminal 跑 `docker ps | grep flux`)。然後 Ctrl+C 結束 tail。

- [ ] **Step 13.6: 從 router 容器內測試 agent reachability**

```bash
docker exec -it anila-platform-dev-router-1 \
    curl -sf -X POST http://flux2-dev-agent:8000/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d '{"model":"image-generator","messages":[{"role":"user","content":"a tank in mountainous terrain, cinematic"}]}'
```

Expected: JSON response,`choices[0].message.content` 含 `![](/uploads/flux/<uuid>.png)`。整個請求約 30-60 秒。

- [ ] **Step 13.7: 從 host 確認圖檔真的落地**

```bash
ls -la /home/aia/c1147259/ANILA/share-dev/uploads/flux/
```

Expected: 看到 step 13.6 產生的 `<uuid>.png` 檔。

- [ ] **Step 13.8: 透過 nginx 從外部拿圖**

```bash
# 從 Step 13.6 response 抓出 URL,例如 /uploads/flux/abcd1234.png
curl -sf http://localhost:8080/uploads/flux/<uuid>.png -o /tmp/e2e.png
file /tmp/e2e.png
```

Expected: `/tmp/e2e.png: PNG image data, ...`

- [ ] **Step 13.9: 確認 CSP 已自動註冊 image-generator agent**

```bash
docker compose -f docker-compose-dev.yml restart csp
sleep 15
docker exec -it anila-platform-dev-router-1 curl -sf http://csp:8000/v1/agents \
    -H "Authorization: Bearer ${SMOKE_USER_API_KEY_DEV:-sk-test-dev-user-api-key}" \
    | python -m json.tool
```

Expected: response 含一筆 `id: image-generator`,`endpoint_url: http://flux2-dev-agent:8000`,且有 `description_for_router` 欄位。

- [ ] **Step 13.10: 從 UI 端 trigger dispatch — 用 Router 而非直接打 agent**

```bash
docker exec -it anila-platform-dev-router-1 \
    curl -sf -X POST http://localhost:9000/v1/chat/completions \
    -H "Authorization: Bearer ${SMOKE_USER_API_KEY_DEV:-sk-test-dev-user-api-key}" \
    -H "Content-Type: application/json" \
    -d '{"model":"gemma4","messages":[{"role":"user","content":"幫我畫一張在山上的坦克"}]}'
```

Expected: response 中可看到 markdown image link `![](/uploads/flux/...)`。這代表 gemma4 正確 DISPATCH 到 `image-generator`,整條鏈通了。

- [ ] **Step 13.11: 從瀏覽器人工驗證 ANILA UI**

打開 anila-ui dev URL(例如 `https://localhost:8443`),登入,在聊天框打「幫我畫一張在山上的坦克」,確認:
1. Loading state 在等 60 秒內出現
2. Assistant 訊息 inline 顯示一張圖
3. 點圖會放大或在新分頁打開

- [ ] **Step 13.12: 寫入 README 更新(可選但建議)**

開 `models/docker-compose.yml` 開頭的註解區塊,在「Endpoint URLs registered in CSP model_registry」那段加一行:

```
#     http://flux2-dev-agent:8000  (model_type=agent, image-generator)
```

- [ ] **Step 13.13: 最終 commit**

```bash
git add models/docker-compose.yml share-dev/uploads/flux/.gitkeep
git commit -m "docs(models): document flux2-dev-agent in compose header"
```

---

## Verification Checklist

完成所有 task 後,在 main session 中確認:

- [ ] `pytest models/flux2-dev-agent/tests/ -v` — 全綠
- [ ] `pytest models/flux2-dev/tests/ -v` — 全綠
- [ ] `docker compose -f models/docker-compose.yml config --quiet` — 無錯
- [ ] `docker compose -f docker-compose-dev.yml config --quiet` — 無錯
- [ ] `nvidia-smi` — GPU 1 + 2 有 process 在跑、GPU 0 + 3 不變
- [ ] `curl http://localhost:8080/uploads/flux/<file>.png` — 200 PNG
- [ ] UI 對話「幫我畫一張…」 — 圖 inline 出現
- [ ] Router log 顯示 `DISPATCH:image-generator:...`
- [ ] CSP audit log 含 image-generator 的 chat completion entry
- [ ] (License gate)BFL 授權確認完成 — 若否,**回滾本計畫**

---

## Rollback Plan

如果上線發現問題:

```bash
# 停掉 FLUX,其他服務不動
docker compose -f models/docker-compose.yml stop flux2-dev-agent flux2-dev
docker compose -f models/docker-compose.yml rm -f flux2-dev-agent flux2-dev

# 從 CSP 移除 agent
docker compose -f docker-compose-dev.yml exec csp \
    curl -X DELETE http://localhost:8000/api/models/image-generator \
    -H "X-CSP-Service-Token: ${CSP_SERVICE_TOKEN}"

# 或直接 revert AUTO_REGISTER_MODELS 變更並 restart csp
git revert <task-11-commit-sha>
docker compose -f docker-compose-dev.yml restart csp
```

軍方 UI 端不會看到任何錯誤 — gemma4 看不到 `image-generator` 就會走「直接回答」路徑,跟還沒部署前一樣。

---

## 潛在地雷預警

實作時這幾件事很容易踩雷,先放這裡:

1. **diffusers Flux2Pipeline API**:截至 2026-05,`diffusers ≥ 0.32.0` 才有 Flux2Pipeline,且 `device_map="balanced"` 對雙 GPU 是必要的。如果 import 失敗看 release notes。
2. **`HF_HUB_OFFLINE=1` + bind-mount**:diffusers 還是會嘗試從 HF 下載 tokenizer / config 元資料。要確保 bind-mount 進來的 `FLUX.2-dev` 目錄含完整 `tokenizer/`、`text_encoder/` 子目錄,不只是 `transformer/*.safetensors`。
3. **`device_map="balanced"` 行為**:會自動把 DiT 與文字編碼器分散到兩張卡。如果一張卡記憶體爆掉,改用 `device_map="auto"` 或顯式 dict(`{"transformer": "cuda:0", "text_encoder": "cuda:1", ...}`)。
4. **Long-pending HTTP**:CSP 預設 httpx timeout 是 120s,FLUX inference 30-90 秒沒問題;但如果以後升到 50 step 可能就要調 CSP `timeout` 或 router `timeout`。
5. **Router LLM 派發誤判**:gemma4 看到「畫蛇添足」「描繪一下情境」這種非真的要繪圖的請求可能誤派。觀察 Router 對話樣本,必要時微調 `description_for_router` 文字加入 negative examples。
6. **軍方資安**:Prompt log 預設會經過 Router → CSP → flux-agent → 翻譯 callback gemma4,**整條都有 log**。要在 router/CSP 那層決定遮罩規則,本計畫不處理(out of scope)。
