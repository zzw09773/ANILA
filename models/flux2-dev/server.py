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
