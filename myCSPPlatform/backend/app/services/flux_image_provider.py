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
        """SHA256 hex digest of prompt + aspect_ratio (NUL-joined to
        avoid prompt='ab' aspect='c'/prompt='abc' collisions)."""
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
