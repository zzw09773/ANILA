from __future__ import annotations

import os
from typing import List, Union

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

TRITON_URL = os.environ.get("TRITON_URL", "http://inference_server_6_prod:8000")
MODEL_NAME = os.environ.get("MODEL_NAME", "nv-embed-v2")
REQUEST_TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT", "60"))

app = FastAPI(title="NV-Embed-v2 OpenAI-Compat Proxy")


class EmbeddingRequest(BaseModel):
    model: str | None = None
    input: Union[str, List[str]]
    encoding_format: str | None = "float"


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [{"id": MODEL_NAME, "object": "model", "owned_by": "triton"}],
    }


@app.post("/v1/embeddings")
async def create_embeddings(req: EmbeddingRequest):
    texts = [req.input] if isinstance(req.input, str) else req.input
    if not texts:
        raise HTTPException(status_code=400, detail="input must not be empty")

    triton_req = {
        "inputs": [
            {
                "name": "documents",
                "datatype": "BYTES",
                "shape": [1, len(texts)],
                "data": texts,
            }
        ]
    }

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.post(
                f"{TRITON_URL}/v2/models/{MODEL_NAME}/infer",
                json=triton_req,
            )
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"triton unreachable: {e}")

    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"triton error {resp.status_code}: {resp.text[:300]}",
        )

    body = resp.json()
    output = next(
        (o for o in body.get("outputs", []) if o.get("name") == "embeddings"),
        None,
    )
    if output is None:
        raise HTTPException(status_code=500, detail="triton missing 'embeddings' output")

    flat = output["data"]
    shape = output["shape"]
    if len(shape) != 2 or shape[0] != len(texts):
        raise HTTPException(
            status_code=500,
            detail=f"unexpected output shape {shape} for input batch {len(texts)}",
        )

    n, d = shape
    embeddings = [flat[i * d : (i + 1) * d] for i in range(n)]
    prompt_tokens = sum(max(1, len(t.split())) for t in texts)

    return {
        "object": "list",
        "model": req.model or MODEL_NAME,
        "data": [
            {"object": "embedding", "index": i, "embedding": emb}
            for i, emb in enumerate(embeddings)
        ],
        "usage": {"prompt_tokens": prompt_tokens, "total_tokens": prompt_tokens},
    }
