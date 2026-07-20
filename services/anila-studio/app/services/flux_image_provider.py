"""FluxImageProvider — generate slide illustrations via an OpenAI 相容
Images API 的 FLUX 服務(雲端算力中心統一部署)。

Lives at the CSP-side of the slide pipeline. Called by
``_hydrate_images()`` (api/studio.py) when a slide needs a generated
illustration (Stage 1: the cover hero).

Responsibilities:
  1. Content-addressable cache. Stage 1 locked contract 3.3: the key folds
     in prompt + aspect + **seed + style_id + steps + guidance**, so a
     later stage that changes seed / style / sampler params does NOT get a
     stale cached PNG handed back.
  2. asyncio.Semaphore-limited concurrency — N pptx in flight × M slides
     each could overwhelm the FLUX service.
  3. Direct HTTP to the OpenAI Images API contract:
     ``POST {base}/v1/images/generations`` with
     ``{model, prompt, n, size, response_format:"b64_json"}``
     (+ optional ``Authorization: Bearer``), response
     ``{created, data:[{b64_json}]}``. base URL 有無 ``/v1`` 皆可 —
     比照 csp memory_service._embed:沒帶版本段就補 ``/v1``。
     seed / num_inference_steps / guidance_scale 不是 OpenAI 標準欄位,
     **不上 wire**(僅留在函式簽名供 cache key 與 audit 用)。
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
    """FLUX 服務 returned non-200 or a malformed JSON payload."""


# Stage 1 locked contract 3.4: use_case → aspect ratio mapping lives in
# exactly one place. The hydration layer passes a use_case; the provider
# translates it to the aspect string. Adding a use_case later means adding
# one row here, nowhere else.
_USE_CASE_ASPECT: dict[ImageUseCase, str] = {
    ImageUseCase.COVER_HERO: "16:9",
    ImageUseCase.SECTION_BAND: "3:1",
    ImageUseCase.CONTENT_ILLUSTRATION: "4:3",
}

# OpenAI Images API 只認 ``size`` 字串,不認 aspect ratio。對映表:
#   * 1:1 / 16:9 / 9:16 用 OpenAI(dall-e-3)標準尺寸,相容性最大;
#   * 4:3 / 3:4 / 3:1 沿用 flux2-dev 原生解析度(FLUX ≤0.8MP 穩定區,
#     OpenAI 標準沒有這些比例,相容伺服器普遍接受任意 WxH)。
# 未知 aspect → _DEFAULT_SIZE(fallback 預設)。
_ASPECT_TO_SIZE: dict[str, str] = {
    "1:1": "1024x1024",
    "16:9": "1792x1024",
    "9:16": "1024x1792",
    "4:3": "1216x896",
    "3:4": "896x1216",
    "3:1": "1536x512",
}
_DEFAULT_SIZE = "1024x1024"


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
    clip_score: float | None = None  # always None: CLIP de-scoped (2026-05-21); kept for audit schema (spec 3.5)
    vlm_verdict: dict | None = None  # Stage 2 fills
    accepted: bool = True  # Stage 2 gate result


@dataclass
class FluxImageProvider:
    #: OpenAI 相容 FLUX 服務的 base URL(伺服器根或含 /v1 皆可)。
    flux_url: str
    cache_dir: Path
    max_concurrent: int
    timeout_seconds: float = 180.0
    #: OpenAI Images API 的 model 名(request body 的 ``model`` 欄位)。
    model: str = "flux.2-dev"
    #: 有值才帶 ``Authorization: Bearer``;空字串 = 不帶(內網免驗)。
    api_key: str = ""
    #: Formal durable jobs use CSP's task-bound Images seam, never ``flux_url``.
    via_csp_runtime: bool = False
    _semaphore: asyncio.Semaphore | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.cache_dir = Path(self.cache_dir)
        self._semaphore = asyncio.Semaphore(self.max_concurrent)

    def _images_endpoint(self) -> str:
        """回傳正規化後的 ``.../v1/images/generations`` 完整 URL。

        比照 csp memory_service._embed:base 沒帶版本段(/v1、/v2)結尾
        就補 /v1,兩種設定慣例(裸 host / 含 /v1)都吃。
        """
        base = self.flux_url.rstrip("/")
        if not base.endswith(("/v1", "/v2")):
            base = f"{base}/v1"
        return f"{base}/images/generations"

    # ── Cache key (contract 3.3 + 2026-07-06 image-primary hot-swap) ──────
    def _cache_key(
        self,
        prompt: str,
        aspect_ratio: str,
        seed: int,
        style_id: str,
        steps: int | None,
        guidance: float | None,
        model: str,
    ) -> str:
        """SHA256 over (prompt, aspect, seed, style_id, steps, guidance,
        model), NUL-joined so component boundaries can't collide. ``steps``
        / ``guidance`` of None are stringified ("None") — the same default
        always lands on the same key, and switching to an explicit value
        produces a different key (so the cached default isn't reused).

        ``model`` joined 2026-07-06: with the image-primary fetcher, the
        provider backing a given ``cache_dir`` can be rebuilt with a new
        model name at runtime (admin swaps the "主圖像模型" in csp) while
        the on-disk cache directory stays the same. Without folding model
        into the key, the same (prompt, seed, style) tuple would silently
        hand back a PNG generated by the *previous* model."""
        h = hashlib.sha256()
        for part in (
            prompt,
            aspect_ratio,
            str(seed),
            style_id,
            str(steps),
            str(guidance),
            model,
        ):
            h.update(part.encode("utf-8"))
            h.update(b"\x00")
        return h.hexdigest()

    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.png"

    def _seed_sidecar_path(self, key: str) -> Path:
        """Sidecar holding the actual accepted seed for a cached image."""
        return self.cache_dir / f"{key}.seed"

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
        key = self._cache_key(prompt, aspect_ratio, seed, style_id, steps, guidance, self.model)
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

    # ── Candidate generation for the Stage 2 gate (no cache) ──────────────
    async def generate_candidates(
        self,
        prompt: str,
        *,
        use_case: ImageUseCase,
        seed: int,
        num_candidates: int = 2,
        steps: int | None = None,
        guidance: float | None = None,
    ) -> list[GeneratedImage]:
        """Generate N fresh candidates WITHOUT touching the cache.

        Stage 2 (Layer C) architecture choice B: the provider stays a pure
        FLUX HTTP client and does NOT run the quality gate (the gate needs
        the gemma4 VLM client, which lives in api/studio.py with the DB /
        auth / proxy context). The hydration layer drives the retry loop —
        each attempt calls this with a different seed, gates the candidates,
        and persists the winner via ``persist_chosen`` once one is accepted.

        Candidates come back ``accepted=False`` (the gate decides).
        """
        aspect_ratio = _USE_CASE_ASPECT[use_case]
        images, actual_seed = await self._generate(
            prompt,
            aspect_ratio=aspect_ratio,
            seed=seed,
            num_candidates=num_candidates,
            steps=steps,
            guidance=guidance,
        )
        return [
            GeneratedImage(png_bytes=png, seed=actual_seed, accepted=False)
            for png in images
        ]

    def persist_chosen(
        self,
        chosen: GeneratedImage,
        *,
        prompt: str,
        use_case: ImageUseCase,
        seed: int,
        style_id: str = "default",
        steps: int | None = None,
        guidance: float | None = None,
    ) -> None:
        """Cache the gate-accepted candidate under the contract-3.3 key.

        Keyed on the REQUESTED seed (the deterministic per-slide seed), not
        on any per-attempt retry seed, so a re-run of the same job hits the
        cache and returns the same chosen image (Stage 1 acceptance #2).
        """
        aspect_ratio = _USE_CASE_ASPECT[use_case]
        key = self._cache_key(prompt, aspect_ratio, seed, style_id, steps, guidance, self.model)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache_path(key).write_bytes(chosen.png_bytes)
        # Sidecar records the seed that ACTUALLY produced the accepted image
        # (may be a retry-shifted seed != the requested deterministic seed), so
        # cache hits report a reproducible seed in image_gen_meta rather than
        # the request key's seed.
        self._seed_sidecar_path(key).write_text(str(chosen.seed), encoding="utf-8")

    def cached_image(
        self,
        prompt: str,
        *,
        use_case: ImageUseCase,
        seed: int,
        style_id: str = "default",
        steps: int | None = None,
        guidance: float | None = None,
    ) -> GeneratedImage | None:
        """Return the cached gate-accepted image for this key, or None."""
        aspect_ratio = _USE_CASE_ASPECT[use_case]
        key = self._cache_key(prompt, aspect_ratio, seed, style_id, steps, guidance, self.model)
        cache_file = self._cache_path(key)
        if cache_file.exists():
            # Prefer the actual accepted seed from the sidecar; fall back to the
            # requested seed for caches written before the sidecar existed.
            actual_seed = seed
            sidecar = self._seed_sidecar_path(key)
            if sidecar.exists():
                try:
                    actual_seed = int(sidecar.read_text(encoding="utf-8").strip())
                except (ValueError, OSError):
                    actual_seed = seed
            return GeneratedImage(
                png_bytes=cache_file.read_bytes(), seed=actual_seed, accepted=True
            )
        return None

    # ── FLUX service call (OpenAI Images API) ─────────────────────────────
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
        """POST the OpenAI Images API contract to the FLUX service; return
        (list of decoded PNG bytes, seed for audit).

        seed / steps / guidance 不是 OpenAI 標準欄位,不上 wire(僅參與
        cache key 與 audit meta);OpenAI 回應也沒有 seed,故 audit seed
        一律回傳 caller 要求的 seed。
        """
        assert self._semaphore is not None  # set in __post_init__
        size = _ASPECT_TO_SIZE.get(aspect_ratio, _DEFAULT_SIZE)
        body: dict = {
            "model": self.model,
            "prompt": prompt,
            "n": num_candidates,
            "size": size,
            "response_format": "b64_json",
        }
        if seed is not None or steps is not None or guidance is not None:
            logger.debug(
                "OpenAI Images API has no seed/steps/guidance fields; "
                "ignoring on the wire (seed=%s steps=%s guidance=%s)",
                seed, steps, guidance,
            )
        async with self._semaphore:
            if self.via_csp_runtime:
                from app.clients.csp_client import (
                    CspClientError,
                    proxy_image_generations,
                )

                try:
                    data = await proxy_image_generations(
                        model=self.model,
                        prompt=prompt,
                        n=num_candidates,
                        size=size,
                        bearer=self.api_key,
                        timeout_seconds=self.timeout_seconds,
                    )
                except CspClientError as exc:
                    raise FluxBackendError(
                        f"CSP governed Images API failed: {exc}"
                    ) from exc
            else:
                headers: dict[str, str] = {}
                if self.api_key:
                    headers["Authorization"] = f"Bearer {self.api_key}"
                async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                    resp = await client.post(
                        self._images_endpoint(),
                        json=body,
                        headers=headers,
                    )
                if resp.status_code != 200:
                    raise FluxBackendError(
                        f"FLUX images API returned {resp.status_code}: {resp.text[:200]}"
                    )
                try:
                    data = resp.json()
                except ValueError as e:
                    raise FluxBackendError(
                        f"FLUX images API returned non-JSON body: {resp.text[:200]}"
                    ) from e

        items = data.get("data")
        if not isinstance(items, list) or not items:
            raise FluxBackendError(
                f"FLUX images API response missing non-empty 'data' list: "
                f"{str(data)[:200]}"
            )
        try:
            # validate=True：b64decode 預設會靜默丟棄非法字元（"!!!!" → b""），
            # 必須顯式驗證才會 raise，否則壞回應解成空 bytes 還會進 cache。
            png_list = [
                base64.b64decode(item["b64_json"], validate=True) for item in items
            ]
        except (KeyError, TypeError) as e:
            raise FluxBackendError(
                f"FLUX images API response items missing 'b64_json': "
                f"{str(data)[:200]}"
            ) from e
        except (binascii.Error, ValueError) as e:
            raise FluxBackendError(
                f"FLUX images API returned malformed base64 in 'b64_json': {e}"
            ) from e
        if any(not png for png in png_list):
            raise FluxBackendError(
                f"FLUX images API returned empty image payload: {str(data)[:200]}"
            )

        # OpenAI 回應沒有 seed 欄位 → audit 用 caller 要求的 seed。
        return png_list, seed
