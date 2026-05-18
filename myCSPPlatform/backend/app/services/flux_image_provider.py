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
