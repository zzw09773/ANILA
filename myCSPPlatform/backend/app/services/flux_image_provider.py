"""FluxImageProvider — generate slide illustrations via flux2-dev.

Lives at the CSP-side of the slide pipeline. Called by
``_hydrate_images()`` (api/studio.py) when a slide needs a generated
illustration (Stage 1: the cover hero).

Responsibilities:
  1. Content-addressable cache. Stage 1 locked contract 3.3: the key folds
     in prompt + aspect + **seed + style_id + steps + guidance**, so a
     later stage that changes seed / style / sampler params does NOT get a
     stale cached PNG handed back.
  2. asyncio.Semaphore-limited concurrency — N pptx in flight × M slides
     each could overwhelm flux2-dev.
  3. Direct HTTP to the flux2-dev backend's new JSON ``/generate`` contract
     (contract 3.2): sends seed + num_candidates, receives a list of
     base64 PNGs + the real seed + a meta dict.
  4. Always returns ``list[GeneratedImage]`` (contract 3.4) — even for the
     Stage 1 N=1 case — so the Stage 2 N-candidate quality gate can slot in
     without changing the return type or the hydration call sites.
  5. Fail-loud on backend error; caller drops the prompt and the renderer
     falls back to standard layout (same failure path as image_ref).
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from app.schemas.studio import ImageUseCase

logger = logging.getLogger(__name__)


class FluxBackendError(RuntimeError):
    """flux2-dev returned non-200 or a malformed JSON payload."""


# Stage 1 locked contract 3.4: use_case → aspect ratio mapping lives in
# exactly one place. The hydration layer passes a use_case; the provider
# translates it to the aspect string the FLUX service understands. Adding
# a use_case later means adding one row here, nowhere else.
_USE_CASE_ASPECT: dict[ImageUseCase, str] = {
    ImageUseCase.COVER_HERO: "16:9",
    ImageUseCase.SECTION_BAND: "3:1",
    ImageUseCase.CONTENT_ILLUSTRATION: "4:3",
}


@dataclass
class GeneratedImage:
    """One candidate image plus its audit / gate metadata.

    Stage 1 always produces ``accepted=True`` with ``clip_score`` and
    ``vlm_verdict`` left None — the quality gate (Layer C) is a Stage 2
    addition and is a pass-through here. The fields exist now so Stage 2
    fills them in place without reshaping the dataclass.
    """

    png_bytes: bytes
    seed: int
    clip_score: float | None = None  # Stage 2 fills
    vlm_verdict: dict | None = None  # Stage 2 fills
    accepted: bool = True  # Stage 2 gate result


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

    # ── Cache key (contract 3.3) ──────────────────────────────────────────
    def _cache_key(
        self,
        prompt: str,
        aspect_ratio: str,
        seed: int,
        style_id: str,
        steps: int | None,
        guidance: float | None,
    ) -> str:
        """SHA256 over (prompt, aspect, seed, style_id, steps, guidance),
        NUL-joined so component boundaries can't collide. ``steps`` /
        ``guidance`` of None are stringified ("None") — the same default
        always lands on the same key, and switching to an explicit value
        produces a different key (so the cached default isn't reused)."""
        h = hashlib.sha256()
        for part in (
            prompt,
            aspect_ratio,
            str(seed),
            style_id,
            str(steps),
            str(guidance),
        ):
            h.update(part.encode("utf-8"))
            h.update(b"\x00")
        return h.hexdigest()

    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.png"

    # ── Public API (contract 3.4) ─────────────────────────────────────────
    async def get_or_generate(
        self,
        prompt: str,
        *,
        use_case: ImageUseCase,
        seed: int,
        style_id: str = "default",
        num_candidates: int = 1,
        steps: int | None = None,
        guidance: float | None = None,
    ) -> list[GeneratedImage]:
        """Return candidate image(s) for the (prompt, use_case, seed, style)
        tuple. Always a list (Stage 1: length 1).

        Cache: keyed by the full contract-3.3 tuple. A hit reads the PNG
        from disk and returns a single accepted candidate (the cached file
        is the already-chosen image, so N>1 on a hit still yields one). A
        miss calls the FLUX service, persists candidate[0] to the cache,
        and returns every candidate the service produced.
        """
        aspect_ratio = _USE_CASE_ASPECT[use_case]
        key = self._cache_key(prompt, aspect_ratio, seed, style_id, steps, guidance)
        cache_file = self._cache_path(key)
        if cache_file.exists():
            return [
                GeneratedImage(
                    png_bytes=cache_file.read_bytes(),
                    seed=seed,
                    accepted=True,
                )
            ]

        images, actual_seed = await self._generate(
            prompt,
            aspect_ratio=aspect_ratio,
            seed=seed,
            num_candidates=num_candidates,
            steps=steps,
            guidance=guidance,
        )

        candidates = [
            GeneratedImage(png_bytes=png, seed=actual_seed, accepted=True)
            for png in images
        ]

        # Persist the first candidate. Stage 1 is N=1 so this is the image
        # that will be used; Stage 2's gate decides which candidate wins
        # before caching, so caching candidate[0] here is a safe Stage 1
        # default (the gate will re-key its accepted image then).
        if candidates:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            cache_file.write_bytes(candidates[0].png_bytes)

        return candidates

    # ── FLUX service call (contract 3.2 JSON) ─────────────────────────────
    async def _generate(
        self,
        prompt: str,
        *,
        aspect_ratio: str,
        seed: int,
        num_candidates: int,
        steps: int | None,
        guidance: float | None,
    ) -> tuple[list[bytes], int]:
        """POST the new JSON contract to flux2-dev /generate; return
        (list of decoded PNG bytes, actual seed used)."""
        assert self._semaphore is not None  # set in __post_init__
        body: dict = {
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "seed": seed,
            "num_candidates": num_candidates,
        }
        if steps is not None:
            body["num_inference_steps"] = steps
        if guidance is not None:
            body["guidance_scale"] = guidance

        async with self._semaphore:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                resp = await client.post(
                    f"{self.flux_url.rstrip('/')}/generate",
                    json=body,
                )
        if resp.status_code != 200:
            raise FluxBackendError(
                f"flux2-dev returned {resp.status_code}: {resp.text[:200]}"
            )
        try:
            data = resp.json()
        except ValueError as e:
            raise FluxBackendError(
                f"flux2-dev returned non-JSON body: {resp.text[:200]}"
            ) from e

        b64_images = data.get("images")
        if not isinstance(b64_images, list) or not b64_images:
            raise FluxBackendError(
                f"flux2-dev response missing non-empty 'images' list: "
                f"{str(data)[:200]}"
            )
        try:
            png_list = [base64.b64decode(b) for b in b64_images]
        except (binascii.Error, ValueError) as e:
            raise FluxBackendError(
                f"flux2-dev returned malformed base64 in 'images': {e}"
            ) from e

        # `seed` in the response is the real value the service used (it may
        # differ when the caller passed seed=None and the service rolled a
        # random one); fall back to the requested seed if absent.
        actual_seed = data.get("seed", seed)
        try:
            actual_seed = int(actual_seed)
        except (TypeError, ValueError):
            actual_seed = seed
        return png_list, actual_seed
