# ANILALM × FLUX Pptx Image Generation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 ANILALM 簡報生成可以**主動產生新圖**(目前只能引用已 ingest 的圖)。透過在 Slide schema 加 `image_prompt` 欄位,讓 LLM 對需要插圖但知識庫沒有合適圖的 slide 自行寫英文 prompt;CSP `_hydrate_images()`(rename 自 `_hydrate_image_refs()`)在送 spec 給 pptx-renderer 前,直接呼叫 `flux2-dev` 後端把 prompt 生成 PNG 並 inline。

**Architecture:** 沿用既有的 `image_data` data-URL inline 機制 —— 不動 pptx-renderer 一行。整合點在 CSP backend 新增一個 `FluxImageProvider` service,有三個職責:(1) SHA256(prompt+aspect) cache 避免重複生圖;(2) `asyncio.Semaphore` 限制並行(避免 pptx 大批 slide 同時生圖把 GPU 壓垮影響 chat);(3) 失敗 fallback 回 standard layout(跟 image_ref 失敗一樣)。**直接打 `http://flux2-dev:8000/generate`**(內部 docker DNS,bypass flux2-dev-agent shim,因為 pptx LLM 產的 prompt 已是英文、要的是 PNG bytes 不是 markdown URL)。

**Tech Stack:**
- Backend: Python 3.12 (myCSPPlatform 既有)、FastAPI、`httpx.AsyncClient`、`pydantic` v2、`hashlib.sha256` cache key
- 測試:`pytest`、`pytest-asyncio`、`respx`(httpx mock)
- 容器網路:CSP 已在 `anila-models-net`,直接 `http://flux2-dev:8000` 可達
- 不改:`ANILALM/pptx-skill/server.js`(Node renderer)、`flux2-dev` / `flux2-dev-agent`(完全沿用 Phase 1 的 deployment)

**前置條件(非本計畫範圍,需先具備):**
1. Phase 1 FLUX integration 已部署且 `flux2-dev` healthy(`anila-model-flux2-dev` 容器跑著,GPU 1+2 持有模型)
2. CSP 容器在 `anila-models-net`(目前已是)
3. `INGESTION_UPLOAD_DIR`(預設 `/var/anila/ingestion-uploads`)CSP 容器有寫入權限(cache 落在這裡的子目錄)

**File Structure:**

```
myCSPPlatform/backend/
├── app/
│   ├── services/
│   │   └── flux_image_provider.py        # 新增 — FLUX 呼叫 + cache + 並行限制
│   ├── api/
│   │   └── studio.py                     # 改 — rename _hydrate_image_refs → _hydrate_images,加 image_prompt 處理路徑;LLM system prompt 加教學
│   └── schemas/
│       └── studio.py                     # 改 — Slide 加 image_prompt 欄位
└── tests/
    ├── test_flux_image_provider.py       # 新增 — provider 單元測試
    └── test_hydrate_images.py            # 新增 — hydrate 整合測試(含 ref / prompt 混合)
```

決策理由:
- `FluxImageProvider` 拆獨立 service module 而非塞進 `studio.py` —— 因為它有 cache state + semaphore,屬於有 lifecycle 的物件,跟 studio.py 的 stateless functions 不同層。
- 不修 pptx-renderer 一個字 —— `image_data` data URL 機制完全沿用,renderer 不知道圖從哪來。
- 不引入 Redis / external cache —— PNG cache 落本機磁碟即可,跟既有 `share/uploads/ingestion` 同一個 mount。

**簡單組件邊界圖:**

```
[ANILALM UI] → 使用者選「依大綱生成」
        ▼
[CSP /api/studio/generate]
        ▼
[gemma4 LLM] ─emit─► SlidesSpec(每 slide 可帶 image_ref 或 image_prompt)
        ▼
[CSP _hydrate_images()]
   ├─ slide.image_ref → 原有路徑(查 images_lookup → 讀磁碟 → base64)
   └─ slide.image_prompt(且無 ref)→ FluxImageProvider.get_or_generate(prompt, aspect)
                                      │
                                      ├─ cache hit → 讀檔 → base64
                                      └─ cache miss → asyncio.Semaphore → POST http://flux2-dev:8000/generate
                                                                          → 寫 cache 檔 → base64
        ▼
[pptx-renderer /render]   ◄── spec 帶完整 image_data data-URLs
        ▼
[PPTX 檔]
```

---

## Task 1: Add `image_prompt` field to Slide schema

**Files:**
- Modify: `myCSPPlatform/backend/app/schemas/studio.py` (Slide class around line 133-157)
- Test: `myCSPPlatform/backend/tests/test_slide_image_prompt.py` (new)

`image_prompt` 是 LLM 對「需要圖但 KB 沒有合適圖」時填的英文 prompt。max_length=500 避免 LLM 寫太長吃 token。跟 `image_ref` 互斥(各自獨立 nullable);hydration 邏輯處理優先序。

- [ ] **Step 1.1: Write failing test**

Create `myCSPPlatform/backend/tests/test_slide_image_prompt.py`:

```python
"""Slide.image_prompt — new field for LLM-requested generated images."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.studio import Slide


def test_slide_accepts_image_prompt():
    s = Slide(
        title="Tank operations",
        bullets=["Mountain patrol", "Engagement protocol"],
        image_prompt="A military tank patrolling mountainous terrain, cinematic, photorealistic",
    )
    assert s.image_prompt is not None
    assert "tank" in s.image_prompt.lower()


def test_slide_image_prompt_defaults_to_none():
    s = Slide(title="X", bullets=["a"])
    assert s.image_prompt is None


def test_slide_can_have_both_ref_and_prompt():
    """Schema allows both — hydration logic decides priority."""
    s = Slide(
        title="X",
        bullets=["a"],
        image_ref="img-abc123",
        image_prompt="A backup illustration if ref fails",
    )
    assert s.image_ref == "img-abc123"
    assert s.image_prompt is not None


def test_slide_rejects_too_long_image_prompt():
    with pytest.raises(ValidationError):
        Slide(
            title="X",
            bullets=["a"],
            image_prompt="A" * 501,  # > max_length=500
        )
```

- [ ] **Step 1.2: Run test to verify it fails**

```bash
cd /home/aia/c1147259/ANILA/myCSPPlatform/backend
pytest tests/test_slide_image_prompt.py -v
```

Expected: tests fail with errors about `image_prompt` being an unknown field (pydantic v2 may either error or silently drop depending on `model_config`).

- [ ] **Step 1.3: Add field to Slide**

Edit `myCSPPlatform/backend/app/schemas/studio.py`. After line 157 (the existing `image_ref` field), add:

```python
    # Phase 6 (2026-05-18): English prompt that the LLM emits when it
    # wants a freshly generated illustration (FLUX.2-dev). Mutually
    # exclusive with image_ref by convention — if both are set, the
    # hydration layer prefers image_ref. Length capped at 500 to avoid
    # LLMs writing essays. Hydration silently drops on FLUX failure
    # (same fallback as image_ref).
    image_prompt: str | None = Field(default=None, max_length=500)
```

- [ ] **Step 1.4: Run tests to verify pass**

```bash
pytest tests/test_slide_image_prompt.py -v
```

Expected: `4 passed`.

- [ ] **Step 1.5: Commit**

```bash
git add myCSPPlatform/backend/app/schemas/studio.py myCSPPlatform/backend/tests/test_slide_image_prompt.py
git commit -m "feat(studio): add Slide.image_prompt for LLM-requested generated images"
```

---

## Task 2: FluxImageProvider — skeleton + cache key computation

**Files:**
- Create: `myCSPPlatform/backend/app/services/flux_image_provider.py`
- Test: `myCSPPlatform/backend/tests/test_flux_image_provider.py`

從最簡單的部分開始:cache key 計算(SHA256(prompt + aspect_ratio))+ 構造子 + cache dir 設定。先不打 FLUX。

- [ ] **Step 2.1: Write failing test**

Create `myCSPPlatform/backend/tests/test_flux_image_provider.py`:

```python
"""FluxImageProvider — generate images via flux2-dev with cache + concurrency limit."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.services.flux_image_provider import FluxImageProvider


def test_provider_construction(tmp_path: Path):
    p = FluxImageProvider(
        flux_url="http://flux2-dev:8000",
        cache_dir=tmp_path / "flux-cache",
        max_concurrent=4,
        timeout_seconds=180.0,
    )
    assert p.cache_dir == tmp_path / "flux-cache"


def test_cache_key_is_deterministic():
    p = FluxImageProvider(
        flux_url="http://flux2-dev:8000",
        cache_dir=Path("/tmp"),
        max_concurrent=4,
    )
    k1 = p._cache_key("a tank in the mountains", "16:9")
    k2 = p._cache_key("a tank in the mountains", "16:9")
    assert k1 == k2
    assert len(k1) == 64  # SHA256 hex
    assert all(c in "0123456789abcdef" for c in k1)


def test_cache_key_differs_on_prompt():
    p = FluxImageProvider(
        flux_url="http://flux2-dev:8000",
        cache_dir=Path("/tmp"),
        max_concurrent=4,
    )
    assert p._cache_key("prompt A", "16:9") != p._cache_key("prompt B", "16:9")


def test_cache_key_differs_on_aspect():
    p = FluxImageProvider(
        flux_url="http://flux2-dev:8000",
        cache_dir=Path("/tmp"),
        max_concurrent=4,
    )
    assert p._cache_key("same prompt", "16:9") != p._cache_key("same prompt", "1:1")


def test_cache_path_uses_key_as_filename(tmp_path: Path):
    p = FluxImageProvider(
        flux_url="http://flux2-dev:8000",
        cache_dir=tmp_path,
        max_concurrent=4,
    )
    path = p._cache_path("a prompt", "16:9")
    assert path.parent == tmp_path
    assert path.suffix == ".png"
    assert path.stem == p._cache_key("a prompt", "16:9")
```

- [ ] **Step 2.2: Run test to verify fails**

```bash
pytest tests/test_flux_image_provider.py -v
```

Expected: `ModuleNotFoundError: No module named 'app.services.flux_image_provider'`.

- [ ] **Step 2.3: Write minimal implementation**

Create `myCSPPlatform/backend/app/services/flux_image_provider.py`:

```python
"""FluxImageProvider — generate slide illustrations via flux2-dev.

Lives at the CSP-side of the slide pipeline. Called by
``_hydrate_images()`` (api/studio.py) when a slide has
``image_prompt`` set but no ``image_ref``.

Responsibilities:
  1. SHA256(prompt + aspect_ratio) keyed cache — same prompt reuses
     PNG, key for repeated generation runs and for slides that
     happen to share a prompt.
  2. asyncio.Semaphore-limited concurrency — N pptx in flight × M
     slides each could overwhelm flux2-dev (one GPU pipeline).
  3. Direct HTTP to flux2-dev backend (bypasses the chat-only
     flux2-dev-agent shim because we want raw PNG bytes not
     markdown URLs).
  4. Fail-loud on backend error; caller drops image_prompt and
     renderer falls back to standard layout (same as image_ref
     failure path).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class FluxImageProvider:
    flux_url: str
    cache_dir: Path
    max_concurrent: int
    timeout_seconds: float = 180.0
    _semaphore: object = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.cache_dir = Path(self.cache_dir)

    def _cache_key(self, prompt: str, aspect_ratio: str) -> str:
        """SHA256 hex digest of prompt + aspect_ratio (NUL-joined to
        avoid prompt='ab' aspect='c'/prompt='abc' collisions)."""
        h = hashlib.sha256()
        h.update(prompt.encode("utf-8"))
        h.update(b"\x00")
        h.update(aspect_ratio.encode("utf-8"))
        return h.hexdigest()

    def _cache_path(self, prompt: str, aspect_ratio: str) -> Path:
        return self.cache_dir / f"{self._cache_key(prompt, aspect_ratio)}.png"
```

- [ ] **Step 2.4: Run tests to verify pass**

```bash
pytest tests/test_flux_image_provider.py -v
```

Expected: `5 passed`.

- [ ] **Step 2.5: Commit**

```bash
git add myCSPPlatform/backend/app/services/flux_image_provider.py myCSPPlatform/backend/tests/test_flux_image_provider.py
git commit -m "feat(studio): FluxImageProvider skeleton + cache key derivation"
```

---

## Task 3: FluxImageProvider — `get_or_generate()` with cache hit + miss

**Files:**
- Modify: `myCSPPlatform/backend/app/services/flux_image_provider.py`
- Modify: `myCSPPlatform/backend/tests/test_flux_image_provider.py` (append tests)

加 `get_or_generate(prompt, aspect_ratio) -> bytes` method:cache hit 直接讀檔回 bytes;cache miss 打 FLUX、寫檔、回 bytes。

- [ ] **Step 3.1: Write failing test**

Append to `myCSPPlatform/backend/tests/test_flux_image_provider.py`:

```python
import httpx
import pytest
import respx


_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


@pytest.mark.asyncio
async def test_cache_hit_returns_existing_file(tmp_path):
    p = FluxImageProvider(
        flux_url="http://flux2-dev:8000",
        cache_dir=tmp_path,
        max_concurrent=4,
    )
    # Pre-populate cache for the expected key
    cache_file = p._cache_path("preexisting prompt", "16:9")
    tmp_path.mkdir(exist_ok=True)
    cache_file.write_bytes(_PNG)

    out = await p.get_or_generate("preexisting prompt", "16:9")
    assert out == _PNG


@pytest.mark.asyncio
@respx.mock
async def test_cache_miss_calls_flux_and_writes_file(tmp_path):
    respx.post("http://flux2-dev:8000/generate").mock(
        return_value=httpx.Response(200, content=_PNG, headers={"content-type": "image/png"})
    )

    p = FluxImageProvider(
        flux_url="http://flux2-dev:8000",
        cache_dir=tmp_path,
        max_concurrent=4,
    )
    out = await p.get_or_generate("fresh prompt", "16:9")

    assert out == _PNG
    cache_file = p._cache_path("fresh prompt", "16:9")
    assert cache_file.exists()
    assert cache_file.read_bytes() == _PNG


@pytest.mark.asyncio
@respx.mock
async def test_subsequent_call_hits_cache(tmp_path):
    route = respx.post("http://flux2-dev:8000/generate").mock(
        return_value=httpx.Response(200, content=_PNG, headers={"content-type": "image/png"})
    )

    p = FluxImageProvider(
        flux_url="http://flux2-dev:8000",
        cache_dir=tmp_path,
        max_concurrent=4,
    )
    await p.get_or_generate("repeated prompt", "16:9")
    await p.get_or_generate("repeated prompt", "16:9")

    # FLUX should have been called exactly once (second was cache hit)
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_cache_miss_sends_correct_body(tmp_path):
    route = respx.post("http://flux2-dev:8000/generate").mock(
        return_value=httpx.Response(200, content=_PNG, headers={"content-type": "image/png"})
    )

    p = FluxImageProvider(
        flux_url="http://flux2-dev:8000",
        cache_dir=tmp_path,
        max_concurrent=4,
    )
    await p.get_or_generate("body test", "1:1")

    import json
    body = json.loads(route.calls.last.request.content)
    assert body == {"prompt": "body test", "aspect_ratio": "1:1"}


@pytest.mark.asyncio
@respx.mock
async def test_creates_cache_dir_if_missing(tmp_path):
    target = tmp_path / "does" / "not" / "exist"
    respx.post("http://flux2-dev:8000/generate").mock(
        return_value=httpx.Response(200, content=_PNG, headers={"content-type": "image/png"})
    )

    p = FluxImageProvider(
        flux_url="http://flux2-dev:8000",
        cache_dir=target,
        max_concurrent=4,
    )
    await p.get_or_generate("any", "16:9")

    assert target.is_dir()
```

- [ ] **Step 3.2: Run test to verify fails**

```bash
pytest tests/test_flux_image_provider.py -v
```

Expected: `AttributeError: 'FluxImageProvider' object has no attribute 'get_or_generate'`.

- [ ] **Step 3.3: Implement get_or_generate**

Edit `myCSPPlatform/backend/app/services/flux_image_provider.py`. Replace the whole file content with:

```python
"""FluxImageProvider — generate slide illustrations via flux2-dev.

Lives at the CSP-side of the slide pipeline. Called by
``_hydrate_images()`` (api/studio.py) when a slide has
``image_prompt`` set but no ``image_ref``.

Responsibilities:
  1. SHA256(prompt + aspect_ratio) keyed cache — same prompt reuses
     PNG, key for repeated generation runs and for slides that
     happen to share a prompt.
  2. asyncio.Semaphore-limited concurrency — N pptx in flight × M
     slides each could overwhelm flux2-dev (one GPU pipeline).
  3. Direct HTTP to flux2-dev backend (bypasses the chat-only
     flux2-dev-agent shim because we want raw PNG bytes not
     markdown URLs).
  4. Fail-loud on backend error; caller drops image_prompt and
     renderer falls back to standard layout (same as image_ref
     failure path).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)


class FluxBackendError(RuntimeError):
    """flux2-dev returned non-200 or wrong content type."""


@dataclass
class FluxImageProvider:
    flux_url: str
    cache_dir: Path
    max_concurrent: int
    timeout_seconds: float = 180.0
    _semaphore: asyncio.Semaphore | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.cache_dir = Path(self.cache_dir)
        self._semaphore = asyncio.Semaphore(self.max_concurrent)

    def _cache_key(self, prompt: str, aspect_ratio: str) -> str:
        h = hashlib.sha256()
        h.update(prompt.encode("utf-8"))
        h.update(b"\x00")
        h.update(aspect_ratio.encode("utf-8"))
        return h.hexdigest()

    def _cache_path(self, prompt: str, aspect_ratio: str) -> Path:
        return self.cache_dir / f"{self._cache_key(prompt, aspect_ratio)}.png"

    async def get_or_generate(self, prompt: str, aspect_ratio: str) -> bytes:
        """Return PNG bytes for (prompt, aspect_ratio). Cache hit → read
        from disk; cache miss → call flux2-dev, write to cache, return.
        """
        cache_file = self._cache_path(prompt, aspect_ratio)
        if cache_file.exists():
            return cache_file.read_bytes()

        png_bytes = await self._generate(prompt, aspect_ratio)

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file.write_bytes(png_bytes)
        return png_bytes

    async def _generate(self, prompt: str, aspect_ratio: str) -> bytes:
        """Call flux2-dev /generate; return PNG bytes."""
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            resp = await client.post(
                f"{self.flux_url.rstrip('/')}/generate",
                json={"prompt": prompt, "aspect_ratio": aspect_ratio},
            )
        if resp.status_code != 200:
            raise FluxBackendError(
                f"flux2-dev returned {resp.status_code}: {resp.text[:200]}"
            )
        ctype = resp.headers.get("content-type", "")
        if not ctype.startswith("image/png"):
            raise FluxBackendError(
                f"flux2-dev unexpected content-type: {ctype!r}"
            )
        return resp.content
```

- [ ] **Step 3.4: Run tests to verify pass**

```bash
pytest tests/test_flux_image_provider.py -v
```

Expected: `10 passed` (5 from Task 2 + 5 new).

- [ ] **Step 3.5: Commit**

```bash
git add myCSPPlatform/backend/app/services/flux_image_provider.py myCSPPlatform/backend/tests/test_flux_image_provider.py
git commit -m "feat(studio): FluxImageProvider.get_or_generate with on-disk cache"
```

---

## Task 4: FluxImageProvider — concurrency limit + error handling

**Files:**
- Modify: `myCSPPlatform/backend/app/services/flux_image_provider.py`
- Modify: `myCSPPlatform/backend/tests/test_flux_image_provider.py` (append tests)

加 semaphore 把 `_generate()` 包起來,確保最多 N 個 concurrent FLUX call。加完整錯誤路徑覆蓋:non-200、wrong content-type。

- [ ] **Step 4.1: Write failing test**

Append to `myCSPPlatform/backend/tests/test_flux_image_provider.py`:

```python
@pytest.mark.asyncio
@respx.mock
async def test_raises_flux_backend_error_on_non_200(tmp_path):
    from app.services.flux_image_provider import FluxBackendError

    respx.post("http://flux2-dev:8000/generate").mock(
        return_value=httpx.Response(500, json={"detail": "OOM"})
    )
    p = FluxImageProvider(
        flux_url="http://flux2-dev:8000",
        cache_dir=tmp_path,
        max_concurrent=4,
    )
    with pytest.raises(FluxBackendError, match="500"):
        await p.get_or_generate("oom test", "16:9")


@pytest.mark.asyncio
@respx.mock
async def test_raises_on_wrong_content_type(tmp_path):
    from app.services.flux_image_provider import FluxBackendError

    respx.post("http://flux2-dev:8000/generate").mock(
        return_value=httpx.Response(200, content=b"not a png", headers={"content-type": "text/plain"})
    )
    p = FluxImageProvider(
        flux_url="http://flux2-dev:8000",
        cache_dir=tmp_path,
        max_concurrent=4,
    )
    with pytest.raises(FluxBackendError, match="content-type"):
        await p.get_or_generate("ct test", "16:9")


@pytest.mark.asyncio
async def test_semaphore_limits_concurrent_calls(tmp_path):
    """N concurrent get_or_generate calls — at most max_concurrent
    in flight at any point. Stubs flux2-dev with a slow handler
    that records overlap."""
    import asyncio

    in_flight = 0
    max_in_flight = 0
    _PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16

    async def slow_handler(request):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        return httpx.Response(200, content=_PNG, headers={"content-type": "image/png"})

    with respx.mock(base_url="http://flux2-dev:8000") as router:
        router.post("/generate").mock(side_effect=slow_handler)

        p = FluxImageProvider(
            flux_url="http://flux2-dev:8000",
            cache_dir=tmp_path,
            max_concurrent=2,
        )
        # Launch 6 concurrent UNIQUE prompts (so cache always misses)
        prompts = [f"prompt-{i}" for i in range(6)]
        await asyncio.gather(*[p.get_or_generate(pr, "16:9") for pr in prompts])

    assert max_in_flight <= 2, f"max_in_flight={max_in_flight}, expected <= 2"


@pytest.mark.asyncio
@respx.mock
async def test_cache_failure_propagates(tmp_path):
    """If flux fails on first call, error propagates and cache is not
    populated — next call must retry."""
    from app.services.flux_image_provider import FluxBackendError

    # First call: 500
    # Second call: 200
    _PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    route = respx.post("http://flux2-dev:8000/generate").mock(
        side_effect=[
            httpx.Response(500, json={"detail": "warmup"}),
            httpx.Response(200, content=_PNG, headers={"content-type": "image/png"}),
        ]
    )
    p = FluxImageProvider(
        flux_url="http://flux2-dev:8000",
        cache_dir=tmp_path,
        max_concurrent=4,
    )

    with pytest.raises(FluxBackendError):
        await p.get_or_generate("retry test", "16:9")
    # Cache should NOT exist after a failed call
    assert not p._cache_path("retry test", "16:9").exists()

    # Retry should succeed
    out = await p.get_or_generate("retry test", "16:9")
    assert out == _PNG
    assert route.call_count == 2
```

- [ ] **Step 4.2: Run tests — concurrency one will fail**

```bash
pytest tests/test_flux_image_provider.py::test_semaphore_limits_concurrent_calls -v
```

Expected: fails (e.g., `max_in_flight=6`, current code has semaphore object but doesn't USE it).

- [ ] **Step 4.3: Wrap _generate in semaphore**

Edit `app/services/flux_image_provider.py`. Replace the `_generate` method (the one written in Task 3) with a version that acquires the semaphore:

```python
    async def _generate(self, prompt: str, aspect_ratio: str) -> bytes:
        """Call flux2-dev /generate (semaphore-limited); return PNG bytes."""
        assert self._semaphore is not None  # set in __post_init__
        async with self._semaphore:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                resp = await client.post(
                    f"{self.flux_url.rstrip('/')}/generate",
                    json={"prompt": prompt, "aspect_ratio": aspect_ratio},
                )
            if resp.status_code != 200:
                raise FluxBackendError(
                    f"flux2-dev returned {resp.status_code}: {resp.text[:200]}"
                )
            ctype = resp.headers.get("content-type", "")
            if not ctype.startswith("image/png"):
                raise FluxBackendError(
                    f"flux2-dev unexpected content-type: {ctype!r}"
                )
            return resp.content
```

- [ ] **Step 4.4: Run all provider tests**

```bash
pytest tests/test_flux_image_provider.py -v
```

Expected: `14 passed`.

- [ ] **Step 4.5: Commit**

```bash
git add myCSPPlatform/backend/app/services/flux_image_provider.py myCSPPlatform/backend/tests/test_flux_image_provider.py
git commit -m "feat(studio): semaphore-limit concurrent FLUX calls + error coverage"
```

---

## Task 5: Rename `_hydrate_image_refs` → `_hydrate_images` and add prompt path

**Files:**
- Modify: `myCSPPlatform/backend/app/api/studio.py` (around lines 841-895 and the call site at 921)
- Create: `myCSPPlatform/backend/tests/test_hydrate_images.py`

Rename 函式並擴充:if `image_ref` 存在,走原路徑;else if `image_prompt` 存在,call FluxImageProvider;失敗時也 pop 欄位讓 renderer fallback。

- [ ] **Step 5.1: Write failing test**

Create `myCSPPlatform/backend/tests/test_hydrate_images.py`:

```python
"""_hydrate_images: resolve image_ref AND image_prompt into image_data."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.api.studio import _hydrate_images
from app.services.flux_image_provider import FluxBackendError


_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


@pytest.fixture
def upload_dir(tmp_path: Path) -> Path:
    d = tmp_path / "uploads"
    d.mkdir()
    return d


@pytest.fixture
def existing_image(upload_dir: Path) -> dict:
    """One pre-existing ingestion image on disk."""
    p = upload_dir / "img-abc.png"
    p.write_bytes(_PNG)
    return {"img-abc": {"storage_path": "img-abc.png", "mime": "image/png"}}


@pytest.mark.asyncio
async def test_hydrate_image_ref_unchanged_behavior(upload_dir, existing_image):
    """image_ref path still works — Task 5 must not regress Task 1's
    existing tests."""
    flux = AsyncMock()
    flux.get_or_generate.return_value = _PNG  # unused on this slide

    spec = {"slides": [{"title": "X", "bullets": ["a"], "image_ref": "img-abc"}]}
    result = await _hydrate_images(
        spec, existing_image, str(upload_dir), flux_provider=flux, default_aspect="16:9"
    )

    s = result["slides"][0]
    assert "image_data" in s
    assert s["image_data"].startswith("data:image/png;base64,")
    flux.get_or_generate.assert_not_called()


@pytest.mark.asyncio
async def test_hydrate_image_prompt_calls_flux(upload_dir, existing_image):
    flux = AsyncMock()
    flux.get_or_generate.return_value = _PNG

    spec = {"slides": [{"title": "Y", "bullets": ["b"], "image_prompt": "a tank"}]}
    result = await _hydrate_images(
        spec, existing_image, str(upload_dir), flux_provider=flux, default_aspect="16:9"
    )

    s = result["slides"][0]
    assert "image_data" in s
    assert s["image_data"].startswith("data:image/png;base64,")
    flux.get_or_generate.assert_awaited_once_with("a tank", "16:9")


@pytest.mark.asyncio
async def test_image_ref_wins_over_image_prompt(upload_dir, existing_image):
    """If both set, prefer image_ref (existing curated content)."""
    flux = AsyncMock()

    spec = {"slides": [{
        "title": "Z", "bullets": ["c"],
        "image_ref": "img-abc",
        "image_prompt": "should not be called",
    }]}
    result = await _hydrate_images(
        spec, existing_image, str(upload_dir), flux_provider=flux, default_aspect="16:9"
    )

    assert result["slides"][0]["image_data"].startswith("data:image/png;base64,")
    flux.get_or_generate.assert_not_called()


@pytest.mark.asyncio
async def test_flux_failure_drops_image_prompt(upload_dir, existing_image):
    """When FLUX fails, drop image_prompt so renderer falls back to
    standard layout — same fallback as a bad image_ref."""
    flux = AsyncMock()
    flux.get_or_generate.side_effect = FluxBackendError("boom")

    spec = {"slides": [{
        "title": "W", "bullets": ["d"],
        "image_prompt": "this will fail",
    }]}
    result = await _hydrate_images(
        spec, existing_image, str(upload_dir), flux_provider=flux, default_aspect="16:9"
    )

    s = result["slides"][0]
    assert "image_data" not in s
    assert "image_prompt" not in s  # popped


@pytest.mark.asyncio
async def test_no_provider_skips_image_prompt(upload_dir, existing_image):
    """If flux_provider is None (FLUX not configured), prompt path is
    skipped silently — slide falls back to standard layout."""
    spec = {"slides": [{
        "title": "V", "bullets": ["e"],
        "image_prompt": "no provider available",
    }]}
    result = await _hydrate_images(
        spec, existing_image, str(upload_dir), flux_provider=None, default_aspect="16:9"
    )

    s = result["slides"][0]
    assert "image_data" not in s


@pytest.mark.asyncio
async def test_mixed_slides_all_resolved(upload_dir, existing_image):
    """A spec with one image_ref slide, one image_prompt slide, and
    one no-image slide — all three resolved correctly."""
    flux = AsyncMock()
    flux.get_or_generate.return_value = _PNG

    spec = {"slides": [
        {"title": "A", "bullets": ["a"], "image_ref": "img-abc"},
        {"title": "B", "bullets": ["b"], "image_prompt": "new image"},
        {"title": "C", "bullets": ["c"]},
    ]}
    result = await _hydrate_images(
        spec, existing_image, str(upload_dir), flux_provider=flux, default_aspect="16:9"
    )

    assert "image_data" in result["slides"][0]
    assert "image_data" in result["slides"][1]
    assert "image_data" not in result["slides"][2]
    flux.get_or_generate.assert_awaited_once()
```

- [ ] **Step 5.2: Run test to verify it fails**

```bash
pytest tests/test_hydrate_images.py -v
```

Expected: `ImportError: cannot import name '_hydrate_images'` (function still named `_hydrate_image_refs`).

- [ ] **Step 5.3: Rename and extend the function**

In `myCSPPlatform/backend/app/api/studio.py`, replace lines 841-895 (the `_hydrate_image_refs` function) with:

```python
async def _hydrate_images(
    spec_dict: dict[str, Any],
    images_lookup: dict[str, dict[str, Any]],
    upload_dir: str,
    *,
    flux_provider: "FluxImageProvider | None" = None,
    default_aspect: str = "16:9",
) -> dict[str, Any]:
    """Resolve every Slide.image_ref OR image_prompt into inline base64 PNG.

    Order of precedence per slide:
      1. image_ref present and resolvable → inline existing PNG.
      2. image_ref present but unresolvable → drop, fall back to standard.
      3. image_prompt present and flux_provider available → generate via FLUX.
      4. image_prompt present but flux_provider None or FLUX fails → drop, fall back to standard.
      5. Neither set → leave untouched (renderer renders standard layout).

    Failure modes for image_prompt path mirror those of image_ref:
    drop the offending field, log warning, let the renderer's
    image_focus → standard fallback take over.
    """
    import base64

    slides = spec_dict.get("slides") or []
    for slide in slides:
        # Path 1: image_ref (existing behavior — unchanged)
        ref = slide.get("image_ref")
        if ref:
            meta = images_lookup.get(ref)
            if not meta:
                slide.pop("image_ref", None)
                # If a fallback image_prompt is present, try that next
            else:
                try:
                    abs_path = os.path.join(upload_dir, meta["storage_path"])
                    with open(abs_path, "rb") as f:
                        blob = f.read()
                    mime = meta.get("mime") or "image/png"
                    slide["image_data"] = (
                        f"data:{mime};base64,{base64.b64encode(blob).decode('ascii')}"
                    )
                    slide.pop("image_prompt", None)  # ref wins
                    continue
                except OSError as e:
                    logger.warning(
                        "Failed to hydrate image_ref=%s for storage_path=%s: %s — "
                        "falling back to image_prompt if available.",
                        ref, meta.get("storage_path"), e,
                    )
                    slide.pop("image_ref", None)

        # Path 2: image_prompt → call FLUX
        prompt = slide.get("image_prompt")
        if not prompt:
            continue

        if flux_provider is None:
            # FLUX not configured for this deployment. Drop prompt silently.
            slide.pop("image_prompt", None)
            continue

        try:
            png_bytes = await flux_provider.get_or_generate(prompt, default_aspect)
            slide["image_data"] = (
                "data:image/png;base64,"
                + base64.b64encode(png_bytes).decode("ascii")
            )
        except Exception as e:
            logger.warning(
                "FLUX generation failed for prompt=%r: %s — "
                "slide will fall back to standard layout.",
                prompt[:80], e,
            )
            slide.pop("image_prompt", None)

    return spec_dict
```

Then update the import line at the top of `studio.py` to include the type annotation hint (find an existing TYPE_CHECKING block or add one). At the top of the file, after the existing imports, add:

```python
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from app.services.flux_image_provider import FluxImageProvider
```

(Avoid the runtime import to keep startup time unchanged and not require FLUX configured.)

Also update the call site in `_render_pptx` (around line 921). Change:

```python
        spec_dict = _hydrate_image_refs(spec_dict, images_lookup, ingest_upload)
```

To:

```python
        spec_dict = await _hydrate_images(
            spec_dict,
            images_lookup,
            ingest_upload,
            flux_provider=get_flux_provider(),  # see Task 6 wiring
            default_aspect="16:9",
        )
```

For now stub `get_flux_provider()` returning `None` so existing tests don't break. Add this helper at module level (near the imports):

```python
def get_flux_provider() -> "FluxImageProvider | None":
    """Returns a configured FluxImageProvider, or None if not set up.

    Stub for Task 5; Task 6 will wire env vars and a process-singleton.
    """
    return None
```

- [ ] **Step 5.4: Run hydrate tests + existing studio tests**

```bash
pytest tests/test_hydrate_images.py tests/test_studio.py -v 2>&1 | tail -30
```

Expected: 6 passed in new file; existing studio tests still pass (no regression).

If existing tests reference `_hydrate_image_refs` by name, you'll need to update them too. Run:

```bash
grep -rn '_hydrate_image_refs' myCSPPlatform/backend/
```

and rename any references to `_hydrate_images`.

- [ ] **Step 5.5: Commit**

```bash
git add myCSPPlatform/backend/app/api/studio.py myCSPPlatform/backend/tests/test_hydrate_images.py
git commit -m "feat(studio): rename _hydrate_image_refs→_hydrate_images + FLUX prompt path"
```

---

## Task 6: Wire `get_flux_provider()` to env vars

**Files:**
- Modify: `myCSPPlatform/backend/app/api/studio.py` (the `get_flux_provider()` stub from Task 5)
- Test: `myCSPPlatform/backend/tests/test_flux_provider_wiring.py` (new)

Read env vars at import,return a process-global `FluxImageProvider` instance (or None if disabled).

- [ ] **Step 6.1: Write failing test**

Create `myCSPPlatform/backend/tests/test_flux_provider_wiring.py`:

```python
"""get_flux_provider() — env-var wiring for FLUX integration."""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture(autouse=True)
def reset_studio_module():
    """Each test gets a fresh studio module so env changes take effect."""
    import app.api.studio as studio
    yield
    importlib.reload(studio)


def test_provider_is_none_when_env_var_missing(monkeypatch):
    monkeypatch.delenv("FLUX_BACKEND_URL", raising=False)
    import app.api.studio as studio
    importlib.reload(studio)
    assert studio.get_flux_provider() is None


def test_provider_built_when_url_configured(monkeypatch, tmp_path):
    monkeypatch.setenv("FLUX_BACKEND_URL", "http://flux2-dev:8000")
    monkeypatch.setenv("FLUX_CACHE_DIR", str(tmp_path / "flux-cache"))
    monkeypatch.setenv("FLUX_MAX_CONCURRENT", "2")
    import app.api.studio as studio
    importlib.reload(studio)

    p = studio.get_flux_provider()
    assert p is not None
    assert p.flux_url == "http://flux2-dev:8000"
    assert p.max_concurrent == 2


def test_provider_uses_default_cache_dir(monkeypatch):
    monkeypatch.setenv("FLUX_BACKEND_URL", "http://flux2-dev:8000")
    monkeypatch.delenv("FLUX_CACHE_DIR", raising=False)
    import app.api.studio as studio
    importlib.reload(studio)

    p = studio.get_flux_provider()
    assert p is not None
    # Default sits under INGESTION_UPLOAD_DIR
    assert "flux-cache" in str(p.cache_dir)


def test_provider_is_singleton_within_module(monkeypatch, tmp_path):
    """Two calls return the same object — semaphore state shared."""
    monkeypatch.setenv("FLUX_BACKEND_URL", "http://flux2-dev:8000")
    monkeypatch.setenv("FLUX_CACHE_DIR", str(tmp_path / "fc"))
    import app.api.studio as studio
    importlib.reload(studio)

    a = studio.get_flux_provider()
    b = studio.get_flux_provider()
    assert a is b
```

- [ ] **Step 6.2: Run test to verify fails**

```bash
pytest tests/test_flux_provider_wiring.py -v
```

Expected: all fail (`get_flux_provider()` is the Task 5 stub returning None).

- [ ] **Step 6.3: Implement wiring**

In `myCSPPlatform/backend/app/api/studio.py`, replace the stub `get_flux_provider` with:

```python
# Module-level singleton: built once on import (or on first call), held
# until process exit. Shared semaphore inside ensures concurrency
# limit holds across all slide-generation requests.
_FLUX_PROVIDER: "FluxImageProvider | None" = None
_FLUX_PROVIDER_INITIALISED = False


def get_flux_provider() -> "FluxImageProvider | None":
    """Return the configured FluxImageProvider, or None if FLUX
    integration is disabled in this deployment.

    Configuration via env:
      FLUX_BACKEND_URL       (required to enable; e.g. http://flux2-dev:8000)
      FLUX_CACHE_DIR         (default: $INGESTION_UPLOAD_DIR/flux-cache)
      FLUX_MAX_CONCURRENT    (default: 4)
      FLUX_TIMEOUT_SECONDS   (default: 180)
    """
    global _FLUX_PROVIDER, _FLUX_PROVIDER_INITIALISED
    if _FLUX_PROVIDER_INITIALISED:
        return _FLUX_PROVIDER

    flux_url = os.environ.get("FLUX_BACKEND_URL", "").strip()
    if not flux_url:
        _FLUX_PROVIDER_INITIALISED = True
        return None

    from app.services.flux_image_provider import FluxImageProvider

    upload_dir = os.environ.get(
        "INGESTION_UPLOAD_DIR", "/var/anila/ingestion-uploads"
    )
    cache_dir = os.environ.get(
        "FLUX_CACHE_DIR", os.path.join(upload_dir, "flux-cache")
    )
    max_concurrent = int(os.environ.get("FLUX_MAX_CONCURRENT", "4"))
    timeout = float(os.environ.get("FLUX_TIMEOUT_SECONDS", "180"))

    from pathlib import Path
    _FLUX_PROVIDER = FluxImageProvider(
        flux_url=flux_url,
        cache_dir=Path(cache_dir),
        max_concurrent=max_concurrent,
        timeout_seconds=timeout,
    )
    _FLUX_PROVIDER_INITIALISED = True
    logger.info(
        "FluxImageProvider wired: url=%s cache_dir=%s concurrent=%d",
        flux_url, cache_dir, max_concurrent,
    )
    return _FLUX_PROVIDER
```

- [ ] **Step 6.4: Run tests to verify pass**

```bash
pytest tests/test_flux_provider_wiring.py -v
```

Expected: `4 passed`.

- [ ] **Step 6.5: Commit**

```bash
git add myCSPPlatform/backend/app/api/studio.py myCSPPlatform/backend/tests/test_flux_provider_wiring.py
git commit -m "feat(studio): env-driven FluxImageProvider singleton wiring"
```

---

## Task 7: Update LLM system prompts to teach image_prompt

**Files:**
- Modify: `myCSPPlatform/backend/app/api/studio.py` (the LLM system prompt strings around lines 520, 548, 599)

LLM 要知道**何時用 image_ref vs image_prompt**。原則:有合適現有圖就用 `image_ref`;沒合適現有圖但 slide 明顯需要插圖時用 `image_prompt`(英文,描述性,~50-200 字)。

- [ ] **Step 7.1: Find the 3 prompt locations**

```bash
grep -n 'image_focus\|image_ref' myCSPPlatform/backend/app/api/studio.py
```

You should see 3 main locations: ~line 520, ~line 548, ~line 599.

- [ ] **Step 7.2: Update the prompt at ~line 520**

Find a block like:
```python
            "  最高的那張」做 image_focus（layout_kind='image_focus' + 設 image_ref）。",
```

Replace this line and the surrounding context with extended instruction:
```python
            "  最高的那張」做 image_focus（layout_kind='image_focus' + 設 image_ref）。",
            "  若知識庫沒有合適的現有圖,但 slide 主題明顯需要視覺輔助(地形、裝備、流程示意),",
            "  可改設 layout_kind='image_focus' + image_prompt(英文,50-200 字,描述性,",
            "  含主體、場景、構圖、風格),系統會即時生圖。一張 slide 只設 image_ref 或 image_prompt 其一,",
            "  不要同時設兩個。",
```

- [ ] **Step 7.3: Update the prompt at ~line 548**

Find the block describing image_ref usage rules. Add an equivalent line:

```python
            "  image_id 填到 Slide.image_ref。bullets 仍要寫 2-4 條，描述圖之外的",
            # ...existing context...
            "  若知識庫沒有合適圖但 slide 需要插圖,設 image_prompt(英文描述)代替 image_ref。",
```

- [ ] **Step 7.4: Update the prompt at ~line 599**

Find the block requiring `image_id` lookup. Add:

```python
            "並把該行的 image_id 填到 Slide.image_ref。一張圖只應被一張投影片引用；"
            # ...existing context...
            "如果該 slide 找不到合適現有圖卻需要插圖,改設 image_prompt(英文)請求即時生成。",
```

- [ ] **Step 7.5: Test the prompt update doesn't break anything**

LLM prompts are hard to unit-test for content; verify the surrounding parser/dispatcher still works:

```bash
cd /home/aia/c1147259/ANILA/myCSPPlatform/backend
pytest tests/test_studio.py -v -k "prompt or spec" 2>&1 | tail -10
```

Expected: existing studio tests still pass.

- [ ] **Step 7.6: Commit**

```bash
git add myCSPPlatform/backend/app/api/studio.py
git commit -m "feat(studio): teach LLM about image_prompt for on-demand FLUX illustrations"
```

---

## Task 8: Wire CSP env vars in docker-compose

**Files:**
- Modify: `docker-compose-dev.yml` (CSP service env block ~line 60)
- Modify: `docker-compose.yml` (prod CSP env block — Phase 1 already touched this)

加 `FLUX_BACKEND_URL`、`FLUX_CACHE_DIR`、`FLUX_MAX_CONCURRENT` 讓 CSP 啟用 provider。

- [ ] **Step 8.1: Edit docker-compose-dev.yml**

Find the CSP service's `environment:` block (around lines 50-90). Add these lines:

```yaml
      FLUX_BACKEND_URL: ${FLUX_BACKEND_URL:-http://flux2-dev:8000}
      FLUX_MAX_CONCURRENT: "${FLUX_MAX_CONCURRENT:-4}"
      FLUX_TIMEOUT_SECONDS: "${FLUX_TIMEOUT_SECONDS:-180}"
      # FLUX_CACHE_DIR defaults to $INGESTION_UPLOAD_DIR/flux-cache
```

Also confirm the `networks:` list for CSP includes `anila-models-net` (already true in Phase 1).

- [ ] **Step 8.2: Edit docker-compose.yml (prod)**

Same env additions to the production CSP service. Locate it:

```bash
grep -n 'csp:' docker-compose.yml | head -3
```

Add the three env vars to its `environment:` block.

- [ ] **Step 8.3: Validate**

```bash
docker compose -f docker-compose-dev.yml config --quiet
docker compose -f docker-compose.yml config --quiet
```

Both should be silent (no errors).

- [ ] **Step 8.4: Commit**

```bash
git add docker-compose-dev.yml docker-compose.yml
git commit -m "[both] feat(compose): wire FLUX env vars for studio FluxImageProvider"
```

---

## Task 9: End-to-end integration test

**Files:**
- Create: `myCSPPlatform/backend/tests/test_studio_flux_e2e.py`

把所有元件串起來測:給一個有 image_prompt 的 spec,經 hydrate → cache 命中/未命中 → FLUX mock → image_data inline。

- [ ] **Step 9.1: Write the e2e test**

Create `myCSPPlatform/backend/tests/test_studio_flux_e2e.py`:

```python
"""End-to-end: SlidesSpec with image_prompt → hydrated spec ready for renderer."""
from __future__ import annotations

import importlib
from pathlib import Path

import httpx
import pytest
import respx


_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


@pytest.mark.asyncio
@respx.mock
async def test_full_pipeline_with_image_prompt(monkeypatch, tmp_path):
    monkeypatch.setenv("FLUX_BACKEND_URL", "http://flux2-dev:8000")
    monkeypatch.setenv("FLUX_CACHE_DIR", str(tmp_path / "flux-cache"))
    monkeypatch.setenv("FLUX_MAX_CONCURRENT", "2")
    monkeypatch.setenv("INGESTION_UPLOAD_DIR", str(tmp_path / "uploads"))

    import app.api.studio as studio
    importlib.reload(studio)

    respx.post("http://flux2-dev:8000/generate").mock(
        return_value=httpx.Response(200, content=_PNG, headers={"content-type": "image/png"})
    )

    spec = {"slides": [
        {"title": "Mountain Patrol", "bullets": ["a", "b"],
         "image_prompt": "Soldiers patrolling a misty mountain at dawn, cinematic"},
    ]}

    result = await studio._hydrate_images(
        spec, {}, str(tmp_path / "uploads"),
        flux_provider=studio.get_flux_provider(),
        default_aspect="16:9",
    )

    s = result["slides"][0]
    assert s["image_data"].startswith("data:image/png;base64,")
    # The cache file should also exist
    cache = (tmp_path / "flux-cache").iterdir()
    cache_files = list(cache)
    assert len(cache_files) == 1
    assert cache_files[0].suffix == ".png"


@pytest.mark.asyncio
@respx.mock
async def test_three_slides_share_one_cache_entry(monkeypatch, tmp_path):
    """Three slides with the SAME image_prompt should only call FLUX once."""
    monkeypatch.setenv("FLUX_BACKEND_URL", "http://flux2-dev:8000")
    monkeypatch.setenv("FLUX_CACHE_DIR", str(tmp_path / "fc"))
    monkeypatch.setenv("INGESTION_UPLOAD_DIR", str(tmp_path / "u"))

    import app.api.studio as studio
    importlib.reload(studio)

    route = respx.post("http://flux2-dev:8000/generate").mock(
        return_value=httpx.Response(200, content=_PNG, headers={"content-type": "image/png"})
    )

    same_prompt = "A repeating banner illustration for section dividers"
    spec = {"slides": [
        {"title": f"Section {i}", "bullets": ["a"], "image_prompt": same_prompt}
        for i in range(3)
    ]}

    result = await studio._hydrate_images(
        spec, {}, str(tmp_path / "u"),
        flux_provider=studio.get_flux_provider(),
        default_aspect="16:9",
    )

    for s in result["slides"]:
        assert s["image_data"].startswith("data:image/png;base64,")
    assert route.call_count == 1  # cache made 2 of 3 hit


@pytest.mark.asyncio
@respx.mock
async def test_flux_failure_falls_back_silently(monkeypatch, tmp_path):
    monkeypatch.setenv("FLUX_BACKEND_URL", "http://flux2-dev:8000")
    monkeypatch.setenv("FLUX_CACHE_DIR", str(tmp_path / "fc"))
    monkeypatch.setenv("INGESTION_UPLOAD_DIR", str(tmp_path / "u"))

    import app.api.studio as studio
    importlib.reload(studio)

    respx.post("http://flux2-dev:8000/generate").mock(
        return_value=httpx.Response(503, json={"detail": "model loading"})
    )

    spec = {"slides": [
        {"title": "X", "bullets": ["a"], "image_prompt": "will fail"},
    ]}

    result = await studio._hydrate_images(
        spec, {}, str(tmp_path / "u"),
        flux_provider=studio.get_flux_provider(),
        default_aspect="16:9",
    )

    s = result["slides"][0]
    assert "image_data" not in s
    assert "image_prompt" not in s  # popped on failure
```

- [ ] **Step 9.2: Run all backend tests**

```bash
cd /home/aia/c1147259/ANILA/myCSPPlatform/backend
pytest tests/test_studio_flux_e2e.py tests/test_flux_image_provider.py \
       tests/test_hydrate_images.py tests/test_flux_provider_wiring.py \
       tests/test_slide_image_prompt.py -v
```

Expected: 3 (e2e) + 14 (provider) + 6 (hydrate) + 4 (wiring) + 4 (schema) = **31 passed**.

- [ ] **Step 9.3: Commit**

```bash
git add myCSPPlatform/backend/tests/test_studio_flux_e2e.py
git commit -m "test(studio): e2e for FLUX image_prompt hydration pipeline"
```

---

## Task 10: Add README + sync backlog entry

**Files:**
- Create or modify: `myCSPPlatform/README.md` (add a FLUX-image-gen section)
- Modify: `docs/branch-sync-backlog.md`

- [ ] **Step 10.1: Add README section**

Append to `myCSPPlatform/README.md`:

```markdown

## Generated illustrations (FLUX integration, Phase 6)

Slides can request a freshly generated illustration via FLUX.2-dev when
the knowledge base has no suitable existing image. The LLM may set
`Slide.image_prompt` (English, 50–500 chars) on any slide that needs
a visual. The CSP `_hydrate_images` step calls `flux2-dev` directly
via `FluxImageProvider`, caches by SHA256(prompt + aspect_ratio), and
inlines the resulting PNG as a base64 data URL in `image_data` —
which is exactly the same shape that pre-existing `image_ref` produces,
so the pptx-renderer needs no changes.

**Env vars (CSP service):**
- `FLUX_BACKEND_URL` (required to enable; e.g. `http://flux2-dev:8000`)
- `FLUX_CACHE_DIR` (default: `$INGESTION_UPLOAD_DIR/flux-cache`)
- `FLUX_MAX_CONCURRENT` (default: `4`)
- `FLUX_TIMEOUT_SECONDS` (default: `180`)

**Failure handling:** any FLUX backend error drops the prompt silently
and the renderer falls back to standard layout — identical behavior to
a missing `image_ref`.

**Cache invalidation:** none. Prompts are content-addressed; to drop
a cached image, delete the file under `$FLUX_CACHE_DIR`.
```

- [ ] **Step 10.2: Update branch sync backlog**

Edit `docs/branch-sync-backlog.md`. Under "變更紀錄" add a line:

```markdown
- **2026-05-18** — Phase 6: ANILALM × FLUX pptx integration plan written (`docs/superpowers/plans/2026-05-18-anilalm-flux-pptx-integration.md`). 10 tasks, ~31 unit tests. Implementation pending.
```

- [ ] **Step 10.3: Commit**

```bash
git add myCSPPlatform/README.md docs/branch-sync-backlog.md
git commit -m "[both] docs(studio): FLUX image_prompt deployment notes + backlog update"
```

---

## Verification Checklist

When all 10 tasks are done:

- [ ] `pytest myCSPPlatform/backend/tests/test_flux_image_provider.py` — 14 passed
- [ ] `pytest myCSPPlatform/backend/tests/test_hydrate_images.py` — 6 passed
- [ ] `pytest myCSPPlatform/backend/tests/test_flux_provider_wiring.py` — 4 passed
- [ ] `pytest myCSPPlatform/backend/tests/test_slide_image_prompt.py` — 4 passed
- [ ] `pytest myCSPPlatform/backend/tests/test_studio_flux_e2e.py` — 3 passed
- [ ] `pytest myCSPPlatform/backend/tests/test_studio.py` — existing studio tests still pass (no regression)
- [ ] `docker compose -f docker-compose-dev.yml config --quiet` — no errors
- [ ] `docker compose -f docker-compose.yml config --quiet` — no errors
- [ ] Manual E2E (after deploy): create a ppt with explicit image_prompt slide → image inline
- [ ] `flux2-dev` GPU usage doesn't go above the `max_concurrent` limit during burst pptx requests

---

## Rollback Plan

If Phase 6 breaks existing slide generation:

```bash
# 1. Disable FLUX provider at CSP (env-only, no code change)
docker compose -f docker-compose-dev.yml exec csp \
    sh -c 'unset FLUX_BACKEND_URL; kill -HUP 1'

# Or comment out FLUX_BACKEND_URL in docker-compose-*.yml and restart CSP
docker compose -f docker-compose-dev.yml restart csp
```

With `FLUX_BACKEND_URL` unset, `get_flux_provider()` returns `None`, and `_hydrate_images()` silently drops any `image_prompt` → renderer falls back to standard layout. Existing `image_ref` slides keep working untouched. **Pure additive change is fully reversible by env.**

If you need to revert the code commits:

```bash
git revert <task-10-sha>..<task-1-sha>  # in reverse chronological order
```

---

## 已知地雷

1. **第一次 FLUX 呼叫的 cold start** — `flux2-dev` 容器啟動後第一張圖會花 ~30 秒(CUDA graph 編譯)。如果你的 `FLUX_TIMEOUT_SECONDS` 設太低,首批使用者會看到 timeout。建議至少 180s。

2. **Cache 不分用戶** — 兩個不同 collection 的使用者下了相同 prompt 會共享同一張圖。內網場景接受;若日後改成多租戶,把 cache key 加 owner_id 即可。

3. **`image_prompt` 字數上限 500** — LLM 偶爾會超過(寫太多 style modifier)。Pydantic 會 reject,然後 schema validator 那層退回 LLM 重答(或 sanitize 截斷)— 取決於現有 SlidesSpec 的 retry 策略。

4. **沒有 prompt 翻譯** — 跟 flux2-dev-agent 不同,Provider 直接送 LLM 寫的 prompt 給 FLUX。要求 LLM system prompt 必須教 LLM 寫英文(已在 Task 7 處理)。如果 LLM 寫中文,FLUX.2-dev 的 Mistral text encoder 還是能跑,但品質會稍微低於英文 prompt。

5. **磁碟空間** — cache 永遠成長。1024×1024 PNG 平均 1-2 MB。一個簡報 ~10 張圖、一天 100 個簡報、20% cache miss = 每日 ~400 MB 新增。一個月 ~12 GB。長期要加 GC(`flux-cache/` LRU prune > 30 天未存取者)。建議列入後續 task。
