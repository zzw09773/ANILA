from __future__ import annotations

import asyncio
import ipaddress
import math
import os
import re
from numbers import Real
from typing import Any, List, Union
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

TRITON_URL = os.environ.get("TRITON_URL", "http://inference_server_6_prod:8000")
# Triton gRPC targets are deliberately not URLs: a host and port are required,
# and a configured target takes precedence over the legacy HTTP transport.
# Preserve the raw value so startup validation can reject surrounding
# whitespace instead of silently normalising a malformed deployment env.
TRITON_GRPC_URL = os.environ.get("TRITON_GRPC_URL", "") or None
MODEL_NAME = os.environ.get("MODEL_NAME", "nv-embed-v2")
REQUEST_TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT", "60"))
READINESS_TIMEOUT = float(os.environ.get("READINESS_TIMEOUT", "5"))
EMBEDDING_DIMENSION = 4096
# Must stay syntax-equivalent to the Gate 5 checker and anila-security target
# parser.  Never use DNS/socket resolution to classify a deployment target.
AMBIGUOUS_NUMERIC_IPV4_COMPONENT_RE = re.compile(r"(?:[0-9]+|0[xX][0-9A-Fa-f]+)$")

app = FastAPI(title="NV-Embed-v2 OpenAI-Compat Proxy")


class EmbeddingRequest(BaseModel):
    model: str | None = None
    input: Union[str, List[str]]
    encoding_format: str | None = "float"


class TritonUpstreamError(RuntimeError):
    """The Triton endpoint could not complete the request."""


class TritonResponseError(RuntimeError):
    """Triton returned a response that violates the embedding contract."""


class TritonConfigurationError(TritonUpstreamError):
    """The explicitly configured Triton transport target is invalid."""


def _error_text(error: BaseException) -> str:
    message = str(error).strip() or error.__class__.__name__
    return message[:300]


def _normalise_grpc_host(host: str) -> str:
    """Return a canonical IPv4/IPv6/DNS host for a bare gRPC target."""

    if not host or "\\" in host or "%" in host:
        raise ValueError("TRITON_GRPC_URL must contain a canonical host")
    try:
        return ipaddress.ip_address(host).compressed.lower()
    except ValueError:
        pass

    candidate = host[:-1] if host.endswith(".") else host
    if candidate and all(
        AMBIGUOUS_NUMERIC_IPV4_COMPONENT_RE.fullmatch(label)
        for label in candidate.split(".")
    ):
        raise ValueError("TRITON_GRPC_URL contains ambiguous numeric IPv4 syntax")
    if not candidate or ".." in candidate or ":" in candidate:
        raise ValueError("TRITON_GRPC_URL must contain a DNS host")
    try:
        labels = tuple(label.encode("idna").decode("ascii").lower() for label in candidate.split("."))
    except UnicodeError as error:
        raise ValueError("TRITON_GRPC_URL must contain a valid DNS host") from error
    if any(
        not label
        or len(label) > 63
        or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
        for label in labels
    ) or len(".".join(labels)) > 253:
        raise ValueError("TRITON_GRPC_URL must contain a valid DNS host")
    return ".".join(labels)


def _validate_grpc_target(target: str) -> str:
    """Validate and return a Triton gRPC ``host:port`` target.

    ``tritonclient`` accepts a host:port target, not an HTTP URL.  Rejecting
    schemes and paths here is important: silently falling back to HTTP for a
    malformed, explicitly requested gRPC target would hide configuration
    errors and could send requests to the wrong backend.
    """

    if not isinstance(target, str) or not target:
        raise ValueError("TRITON_GRPC_URL must be a non-empty host:port")
    if target != target.strip():
        raise ValueError("TRITON_GRPC_URL must not contain surrounding whitespace")
    value = target
    if any(character.isspace() for character in value):
        raise ValueError("TRITON_GRPC_URL must not contain whitespace")
    if "://" in value or "\\" in value:
        raise ValueError("TRITON_GRPC_URL must be host:port without a scheme")

    try:
        parsed = urlsplit(f"//{value}")
    except ValueError as error:
        raise ValueError("TRITON_GRPC_URL must be a valid host:port") from error
    if parsed.path or parsed.query or parsed.fragment:
        raise ValueError("TRITON_GRPC_URL must not include a URL path or query")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("TRITON_GRPC_URL must not include credentials")
    if not parsed.hostname:
        raise ValueError("TRITON_GRPC_URL must include a host")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("TRITON_GRPC_URL must include a numeric port") from error
    if port is None or not 1 <= port <= 65535:
        raise ValueError("TRITON_GRPC_URL must include a valid port")
    if parsed.netloc.endswith(":"):
        raise ValueError("TRITON_GRPC_URL must include a numeric port")
    host = _normalise_grpc_host(parsed.hostname)
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    host_text = f"[{host}]" if ip is not None and ip.version == 6 else host
    return f"{host_text}:{port}"


def _flatten_data(data: Any) -> list[Any] | None:
    if not isinstance(data, (list, tuple)):
        return None

    flattened: list[Any] = []
    for item in data:
        if isinstance(item, (list, tuple)):
            nested = _flatten_data(item)
            if nested is None:
                return None
            flattened.extend(nested)
        else:
            flattened.append(item)
    return flattened


def _validate_embedding_values(values: list[Any]) -> None:
    for value in values:
        if isinstance(value, bool) or not isinstance(value, Real):
            raise TritonResponseError(
                "triton 'embeddings' output contains a non-real value"
            )
        if not math.isfinite(value):
            raise TritonResponseError(
                "triton 'embeddings' output contains a non-finite value"
            )


def _embeddings_from_shape_data(
    shape: Any, data: Any, batch_size: int
) -> list[list[Any]]:
    if not isinstance(shape, (list, tuple)) or len(shape) != 2:
        raise TritonResponseError(f"unexpected output shape {shape!r}")
    if any(
        not isinstance(dimension, int) or isinstance(dimension, bool)
        for dimension in shape
    ):
        raise TritonResponseError(f"unexpected output shape {shape!r}")

    rows, dimensions = shape
    if rows != batch_size or rows <= 0 or dimensions != EMBEDDING_DIMENSION:
        raise TritonResponseError(
            "unexpected output shape "
            f"{shape!r} for input batch {batch_size}; expected "
            f"[batch, {EMBEDDING_DIMENSION}]"
        )

    flattened = _flatten_data(data)
    expected_size = rows * dimensions
    if flattened is None or len(flattened) != expected_size:
        actual_size = None if flattened is None else len(flattened)
        raise TritonResponseError(
            "triton 'embeddings' output data has "
            f"{actual_size!r} values; expected {expected_size} for shape {shape!r}"
        )
    _validate_embedding_values(flattened)
    return [
        flattened[index * dimensions : (index + 1) * dimensions]
        for index in range(rows)
    ]


def _embeddings_from_grpc_result(result: Any, batch_size: int) -> list[list[Any]]:
    try:
        output = result.as_numpy("embeddings")
    except Exception as error:
        raise TritonResponseError(
            f"triton failed to read 'embeddings' output: {_error_text(error)}"
        ) from error
    if output is None:
        raise TritonResponseError("triton missing 'embeddings' output")

    try:
        shape = tuple(output.shape)
        data = output.tolist()
    except Exception as error:
        raise TritonResponseError(
            f"triton returned an invalid 'embeddings' output: {_error_text(error)}"
        ) from error
    return _embeddings_from_shape_data(shape, data, batch_size)


async def _infer_http(texts: list[str]) -> list[list[Any]]:
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
            response = await client.post(
                f"{TRITON_URL.rstrip('/')}/v2/models/{MODEL_NAME}/infer",
                json=triton_req,
            )
    except httpx.RequestError as error:
        raise TritonUpstreamError(
            f"triton HTTP request failed: {_error_text(error)}"
        ) from error

    if response.status_code != 200:
        raise TritonUpstreamError(
            f"triton HTTP error {response.status_code}: {response.text[:300]}"
        )
    try:
        body = response.json()
    except (TypeError, ValueError) as error:
        raise TritonResponseError(
            f"triton returned invalid JSON: {_error_text(error)}"
        ) from error
    if not isinstance(body, dict):
        raise TritonResponseError("triton response body must be an object")

    outputs = body.get("outputs")
    if not isinstance(outputs, list):
        raise TritonResponseError("triton response is missing an 'outputs' list")
    output = next(
        (
            candidate
            for candidate in outputs
            if isinstance(candidate, dict) and candidate.get("name") == "embeddings"
        ),
        None,
    )
    if output is None:
        raise TritonResponseError("triton missing 'embeddings' output")
    return _embeddings_from_shape_data(
        output.get("shape"), output.get("data"), len(texts)
    )


async def _check_http_readiness() -> None:
    try:
        async with httpx.AsyncClient(timeout=READINESS_TIMEOUT) as client:
            server_response = await client.get(
                f"{TRITON_URL.rstrip('/')}/v2/health/ready"
            )
            if server_response.status_code != 200:
                raise TritonUpstreamError(
                    "triton HTTP server readiness returned status "
                    f"{server_response.status_code}"
                )
            model_response = await client.get(
                f"{TRITON_URL.rstrip('/')}/v2/models/{MODEL_NAME}/ready"
            )
    except httpx.RequestError as error:
        raise TritonUpstreamError(
            f"triton HTTP readiness check failed: {_error_text(error)}"
        ) from error
    if model_response.status_code != 200:
        raise TritonUpstreamError(
            f"triton HTTP model {MODEL_NAME!r} readiness returned status "
            f"{model_response.status_code}"
        )


def _infer_grpc(texts: list[str], target: str) -> list[list[Any]]:
    """Run the synchronous Triton gRPC client in a worker thread.

    The caller invokes this function through ``asyncio.to_thread``.  Keeping
    all ``tritonclient`` imports and blocking calls here makes it explicit that
    no synchronous network operation runs on FastAPI's event loop.
    """

    try:
        endpoint = _validate_grpc_target(target)
    except ValueError as error:
        raise TritonConfigurationError(str(error)) from error

    try:
        import numpy as np
        import tritonclient.grpc as grpcclient
    except ImportError as error:
        raise TritonUpstreamError(
            f"triton gRPC client is unavailable: {_error_text(error)}"
        ) from error

    client = None
    try:
        client = grpcclient.InferenceServerClient(url=endpoint)
        input_documents = grpcclient.InferInput("documents", [1, len(texts)], "BYTES")
        input_documents.set_data_from_numpy(np.asarray([texts], dtype=object))
        output_embeddings = grpcclient.InferRequestedOutput("embeddings")
        result = client.infer(
            model_name=MODEL_NAME,
            inputs=[input_documents],
            outputs=[output_embeddings],
            client_timeout=REQUEST_TIMEOUT,
        )
    except Exception as error:
        raise TritonUpstreamError(
            f"triton gRPC request failed: {_error_text(error)}"
        ) from error
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                # A failed cleanup must not hide the inference result/error.
                pass

    return _embeddings_from_grpc_result(result, len(texts))


def _check_grpc_readiness(target: str) -> None:
    try:
        endpoint = _validate_grpc_target(target)
    except ValueError as error:
        raise TritonConfigurationError(str(error)) from error

    try:
        import tritonclient.grpc as grpcclient
    except ImportError as error:
        raise TritonUpstreamError(
            f"triton gRPC client is unavailable: {_error_text(error)}"
        ) from error

    client = None
    try:
        client = grpcclient.InferenceServerClient(url=endpoint)
        server_ready = client.is_server_ready(client_timeout=READINESS_TIMEOUT)
        model_ready = server_ready and client.is_model_ready(
            model_name=MODEL_NAME,
            client_timeout=READINESS_TIMEOUT,
        )
    except Exception as error:
        raise TritonUpstreamError(
            f"triton gRPC readiness check failed: {_error_text(error)}"
        ) from error
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
    if not server_ready:
        raise TritonUpstreamError("triton gRPC server is not ready")
    if not model_ready:
        raise TritonUpstreamError(f"triton gRPC model {MODEL_NAME!r} is not ready")


@app.on_event("startup")
async def validate_startup_configuration() -> None:
    """Reject an explicitly malformed gRPC target before serving traffic."""

    if TRITON_GRPC_URL:
        try:
            _validate_grpc_target(TRITON_GRPC_URL)
        except ValueError as error:
            raise RuntimeError(f"invalid TRITON_GRPC_URL: {error}") from error


@app.get("/health")
async def health():
    try:
        if TRITON_GRPC_URL:
            await asyncio.to_thread(_check_grpc_readiness, TRITON_GRPC_URL)
        else:
            await _check_http_readiness()
    except TritonUpstreamError as error:
        raise HTTPException(status_code=503, detail=_error_text(error)) from error
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

    try:
        if TRITON_GRPC_URL:
            # tritonclient.grpc.InferenceServerClient is synchronous.  Never
            # run it directly in this async request handler.
            embeddings = await asyncio.to_thread(_infer_grpc, texts, TRITON_GRPC_URL)
        else:
            embeddings = await _infer_http(texts)
    except TritonUpstreamError as error:
        raise HTTPException(status_code=502, detail=_error_text(error)) from error
    except TritonResponseError as error:
        raise HTTPException(status_code=500, detail=_error_text(error)) from error

    prompt_tokens = sum(max(1, len(text.split())) for text in texts)
    return {
        "object": "list",
        "model": req.model or MODEL_NAME,
        "data": [
            {"object": "embedding", "index": i, "embedding": embedding}
            for i, embedding in enumerate(embeddings)
        ],
        "usage": {"prompt_tokens": prompt_tokens, "total_tokens": prompt_tokens},
    }
