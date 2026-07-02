"""flux2-dev: minimal HTTP wrapper around diffusers Flux2Pipeline.

Endpoints:
  GET  /health                                  → {"status": "ok"}
  POST /generate {prompt, aspect_ratio, ...}    → GenerateResponse (JSON)

The pipeline is constructed once at module import and injected into
``build_app``. Tests pass a mock pipeline so we never load the real
weights outside of production runs.

Contract note (ANILA Studio FLUX Stage 1, spec 3.2): /generate returns
JSON (base64 PNG list + audit meta), NOT raw image/png bytes. The list
form is intentional even for N=1 so Stage 2's N-candidate gate does not
force a response-type rewrite. Model-agnostic — same contract applies to
flux2-dev and klein-4B.
"""
from __future__ import annotations

import base64
import io
import logging
import os
import random
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


def build_app(*, pipeline: Any) -> FastAPI:
    app = FastAPI(title="flux2-dev", version="0.1.0")

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

        imgs_b64: list[str] = []
        try:
            for i in range(req.num_candidates):
                # CPU generator: pipeline uses device_map="balanced" (weights
                # sharded across GPUs), so a fixed cuda device is unsafe. A CPU
                # generator gives deterministic, device-independent seeding.
                generator = None
                if torch is not None:
                    generator = torch.Generator(device="cpu").manual_seed(
                        base_seed + i
                    )
                out = pipeline(
                    prompt=req.prompt,
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
