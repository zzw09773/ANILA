"""flux2-dev: minimal HTTP wrapper around diffusers Flux2Pipeline.

Endpoints:
  GET  /health                                  → {"status": "ok"}
  POST /generate {prompt, aspect_ratio, ...}    → GenerateResponse (JSON)
  POST /v1/images/generations                   → OpenAI Images API 相容

The pipeline is constructed once at module import and injected into
``build_app``. Tests pass a mock pipeline so we never load the real
weights outside of production runs.

Contract note (ANILA Studio FLUX Stage 1, spec 3.2): /generate returns
JSON (base64 PNG list + audit meta), NOT raw image/png bytes. The list
form is intentional even for N=1 so Stage 2's N-candidate gate does not
force a response-type rewrite. Model-agnostic — same contract applies to
flux2-dev and klein-4B.

OpenAI 相容端點(2026-07 遷移):院內模型統一部署在雲端算力中心後,
兩個 client(anila-studio FluxImageProvider、flux2-dev-agent FluxClient)
改打標準 ``POST {base}/v1/images/generations``。本 dev 後端新增同款端點
對齊新契約,方便本機開發與測試;既有 ``/generate`` 保留不動(向後相容)。
"""
from __future__ import annotations

import base64
import io
import logging
import os
import random
import re
import time
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

try:  # torch is only present in the GPU runtime, not in syntax/CI envs.
    import torch  # type: ignore
except Exception:  # pragma: no cover - torch absent in dev/test
    torch = None  # type: ignore

logger = logging.getLogger(__name__)

_ASPECT_RATIOS: dict[str, tuple[int, int]] = {
    "1:1": (1024, 1024),
    "16:9": (1408, 768),
    "9:16": (768, 1408),
    "4:3": (1216, 896),
    "3:4": (896, 1216),
    "3:1": (1536, 512),  # section band letterbox (≤0.8MP, FLUX stable zone)
}


class GenerateRequest(BaseModel):
    prompt: str
    aspect_ratio: Literal["1:1", "16:9", "9:16", "4:3", "3:4", "3:1"] = "16:9"
    seed: int | None = None
    num_candidates: int = Field(default=1, ge=1, le=4)
    num_inference_steps: int | None = None
    guidance_scale: float | None = None


class GenerateResponse(BaseModel):
    images: list[str]  # base64 PNG, len == num_candidates
    seed: int  # actual seed used (resolved real value when random, for audit)
    meta: dict  # {steps, guidance, width, height, model_sha}


# ── OpenAI Images API 相容(POST /v1/images/generations)──────────────────

_SIZE_RE = re.compile(r"^(\d{2,4})x(\d{2,4})$")


class ImagesGenerationRequest(BaseModel):
    """標準 OpenAI Images API request(只支援 b64_json 回應格式)。"""

    prompt: str
    model: str | None = None  # 單一模型服務:接受但不路由
    n: int = Field(default=1, ge=1, le=4)
    size: str = "1024x1024"
    response_format: str = "b64_json"


class ImageData(BaseModel):
    b64_json: str


class ImagesGenerationResponse(BaseModel):
    created: int
    data: list[ImageData]


def _parse_size(size: str) -> tuple[int, int]:
    """``"WxH"`` → (width, height)。格式錯誤或超界 → 422。"""
    m = _SIZE_RE.match(size.strip())
    if not m:
        raise HTTPException(
            status_code=422, detail=f"invalid size {size!r}; expected 'WxH'"
        )
    width, height = int(m.group(1)), int(m.group(2))
    if not (64 <= width <= 2048 and 64 <= height <= 2048):
        raise HTTPException(
            status_code=422,
            detail=f"size {size!r} out of supported range 64..2048",
        )
    return width, height


def build_app(*, pipeline: Any) -> FastAPI:
    app = FastAPI(title="flux2-dev", version="0.1.0")

    def _render_pngs_b64(
        *,
        prompt: str,
        width: int,
        height: int,
        steps: int,
        guidance: float,
        base_seed: int,
        n: int,
    ) -> list[str]:
        """共用生成核心:跑 pipeline n 次,回 base64 PNG list。

        /generate 與 /v1/images/generations 都走這裡,行為(CPU generator
        seeding、per-candidate seed+i)完全一致。
        """
        imgs_b64: list[str] = []
        try:
            for i in range(n):
                # CPU generator: pipeline uses device_map="balanced" (weights
                # sharded across GPUs), so a fixed cuda device is unsafe. A CPU
                # generator gives deterministic, device-independent seeding.
                generator = None
                if torch is not None:
                    generator = torch.Generator(device="cpu").manual_seed(
                        base_seed + i
                    )
                out = pipeline(
                    prompt=prompt,
                    width=width,
                    height=height,
                    num_inference_steps=steps,
                    guidance_scale=guidance,
                    generator=generator,
                )
                img = out.images[0]
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                imgs_b64.append(base64.b64encode(buf.getvalue()).decode("ascii"))
        except Exception as exc:
            logger.exception("flux inference failed")
            raise HTTPException(status_code=500, detail=f"inference failed: {exc}")
        return imgs_b64

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/generate", response_model=GenerateResponse)
    def generate(req: GenerateRequest) -> GenerateResponse:
        width, height = _ASPECT_RATIOS[req.aspect_ratio]
        steps = req.num_inference_steps or int(os.environ.get("FLUX_NUM_STEPS", "28"))
        guidance = req.guidance_scale or float(
            os.environ.get("FLUX_GUIDANCE_SCALE", "4.0")
        )
        # Resolve a concrete base seed even when the caller passes None, so
        # the value can be echoed back for audit / reproducibility.
        base_seed = req.seed if req.seed is not None else random.randint(0, 2**32 - 1)

        imgs_b64 = _render_pngs_b64(
            prompt=req.prompt,
            width=width,
            height=height,
            steps=steps,
            guidance=guidance,
            base_seed=base_seed,
            n=req.num_candidates,
        )

        return GenerateResponse(
            images=imgs_b64,
            seed=base_seed,
            meta={
                "steps": steps,
                "guidance": guidance,
                "width": width,
                "height": height,
                "model_sha": os.environ.get("FLUX_MODEL_SHA", ""),
            },
        )

    @app.post("/v1/images/generations", response_model=ImagesGenerationResponse)
    def images_generations(req: ImagesGenerationRequest) -> ImagesGenerationResponse:
        """OpenAI Images API 相容端點。

        ``size`` → 內部 width/height;steps / guidance 走 env 預設
        (FLUX_NUM_STEPS / FLUX_GUIDANCE_SCALE);seed 每次隨機(OpenAI
        契約沒有 seed 欄位)。只支援 response_format="b64_json" —— 本服務
        不架靜態檔案伺服器,無從簽發 url。
        """
        if req.response_format != "b64_json":
            raise HTTPException(
                status_code=400,
                detail="only response_format='b64_json' is supported",
            )
        width, height = _parse_size(req.size)
        steps = int(os.environ.get("FLUX_NUM_STEPS", "28"))
        guidance = float(os.environ.get("FLUX_GUIDANCE_SCALE", "4.0"))
        base_seed = random.randint(0, 2**32 - 1)

        imgs_b64 = _render_pngs_b64(
            prompt=req.prompt,
            width=width,
            height=height,
            steps=steps,
            guidance=guidance,
            base_seed=base_seed,
            n=req.n,
        )
        return ImagesGenerationResponse(
            created=int(time.time()),
            data=[ImageData(b64_json=b) for b in imgs_b64],
        )

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
