"""Thin async HTTP wrapper around csp (control plane).

anila-studio cannot import csp Python modules after the extraction, so
every csp call goes through this module. Each function is a focused
one-trick wrapper that:

- ships a bearer (the caller's JWT) as ``Authorization: Bearer …`` —
  user-on-behalf-of, never service-to-service substitution,
- maps csp's HTTP status codes onto typed exceptions
  (``CspUnauthorizedError`` / ``CspForbiddenError`` /
  ``CspNotFoundError`` / ``CspServerError``),
- retries timeouts / network errors / 5xx on an exponential backoff
  (0.5s, 1s, 2s) up to three additional attempts,
- never retries 4xx — those are caller-fixable and a stale JWT does
  not become valid by waiting,
- never caches the response — caching is a caller concern.

Module-level vs per-call ``httpx.AsyncClient``: the first cut uses
``async with httpx.AsyncClient(...)`` inside every function. That costs
one TLS / TCP setup per call (cheap on the cluster-internal csp link)
in exchange for trivial lifecycle — no FastAPI lifespan hook required,
no leak risk on graceful shutdown, and tests can use ``respx`` without
plumbing fixtures for a shared client. A future optimisation lands a
module-level client behind ``app.main`` lifespan hooks once we have
load numbers justifying the upgrade.

Response handling notes:
- ``search_chunks`` / ``search_images`` drop csp's envelope (``query``
  + ``embedding_model`` + ``embedding_dim`` + ``results``) and surface
  only the flattened hit list. Studio doesn't echo the query and never
  reads the embedding metadata — the dataclasses are the contract.
- ``proxy_chat_completions`` returns the raw OpenAI-shaped dict so
  callers can pick the fields they need (csp adds billing metadata
  alongside the standard choices/usage payload).
- ``fetch_image_blob`` reads the body via ``client.stream`` + ``aread``
  so very large rasters do not need a synchronous ``response.content``
  round-trip if we later swap to chunked iteration.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import settings


logger = logging.getLogger(__name__)


# ── Public dataclasses ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class CollectionMeta:
    """Slimmed projection of csp's ``CollectionResponse``.

    csp's full response carries extra bookkeeping (description,
    chunking_config, document_count, chunk_count, bytes_stored,
    created_at, updated_at) studio does not consume. We surface only
    the fields the studio pipeline currently reads; adding more later
    is forward-compatible because we ignore extra keys.
    """

    id: int
    name: str
    embedding_model: str
    embedding_dim: int
    status: str  # "active" | "archived" | ...
    created_by: int


@dataclass(frozen=True)
class ChunkHit:
    chunk_id: int
    document_id: int
    filename: str
    chunk_key: str
    content: str
    score: float
    metadata: dict
    parent_chunk_id: int | None
    parent_content: str | None
    chunk_type: str  # "leaf" | ...
    chunk_level: int


@dataclass(frozen=True)
class ImageHit:
    image_id: int  # ingestion_images.id BIGSERIAL PK
    document_id: int
    page: int | None
    storage_path: str
    mime: str
    caption: str | None
    filename: str
    score: float


# ── Public exception hierarchy ──────────────────────────────────────────────


class CspClientError(Exception):
    """Base for any csp HTTP failure surfaced by this client."""


class CspUnauthorizedError(CspClientError):
    """401 — JWT expired / invalid; caller should refresh and retry."""


class CspForbiddenError(CspClientError):
    """403 — caller's JWT is valid but lacks permission (cross-user)."""


class CspNotFoundError(CspClientError):
    """404 — resource absent."""


class CspServerError(CspClientError):
    """5xx after retry budget exhausted, or transport failure."""


# ── Retry / timeout constants ───────────────────────────────────────────────


# 3 retries on top of the initial attempt = 4 total tries.
_MAX_ATTEMPTS = 4
_BACKOFFS = (0.5, 1.0, 2.0)


def _default_timeout() -> httpx.Timeout:
    return httpx.Timeout(
        timeout=settings.INTERNAL_TIMEOUT_SECONDS,
        connect=settings.INTERNAL_TIMEOUT_CONNECT,
    )


def _llm_timeout() -> httpx.Timeout:
    """Long-running timeout for csp /v1/chat/completions.

    csp's own httpx.Timeout for the LLM upstream is 120s; if we cap below
    that the deck pipeline 502s on every gemma4 cold-start. See
    ``settings.INTERNAL_LLM_TIMEOUT_SECONDS``.
    """
    return httpx.Timeout(
        timeout=settings.INTERNAL_LLM_TIMEOUT_SECONDS,
        connect=settings.INTERNAL_TIMEOUT_CONNECT,
    )


def _auth_headers(bearer: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {bearer}"}


def _safe_detail(response: httpx.Response) -> str:
    """Extract a human-readable error string without exploding on
    non-JSON bodies (csp can return text/html on infra-level errors)."""
    try:
        payload = response.json()
    except ValueError:
        return response.text or f"HTTP {response.status_code}"
    if isinstance(payload, dict) and "detail" in payload:
        detail = payload["detail"]
        if isinstance(detail, str):
            return detail
        if isinstance(detail, dict):
            message = detail.get("message")
            if isinstance(message, str) and message.strip():
                return message
            return json.dumps(detail, ensure_ascii=False, sort_keys=True)
        return str(detail)
    return str(payload)


def _raise_for_4xx(response: httpx.Response) -> None:
    """Map csp's 4xx responses to the typed exception hierarchy.

    Called only after we've decided the response is < 500. 2xx / 3xx
    fall through silently; 4xx raises the matching typed error.
    """
    status = response.status_code
    if status < 400:
        return
    detail = _safe_detail(response)
    if status == 401:
        raise CspUnauthorizedError(detail)
    if status == 403:
        raise CspForbiddenError(detail)
    if status == 404:
        raise CspNotFoundError(detail)
    # Other 4xx (400, 409, 422 …) — surface as the base class so
    # callers can react without conflating with retryable 5xx errors.
    raise CspClientError(f"csp returned {status}: {detail}")


async def _request(
    method: str,
    url: str,
    *,
    bearer: str,
    json_body: dict[str, Any] | None = None,
    stream: bool = False,
    timeout_override: httpx.Timeout | None = None,
    max_attempts_override: int | None = None,
    extra_headers: dict[str, str] | None = None,
) -> httpx.Response:
    """Issue an HTTP request with retry on 5xx and transport errors.

    Returns a fully-read ``httpx.Response``. ``stream=True`` toggles
    httpx's streaming path (used by ``fetch_image_blob``); the body is
    still drained inside this helper so the caller does not have to
    manage the client lifecycle.

    Retry policy: timeouts / network errors / 5xx → sleep
    ``_BACKOFFS[attempt]`` then retry, up to ``_MAX_ATTEMPTS`` total
    attempts. 4xx → ``_raise_for_4xx`` immediately (no retry — a 401
    will not heal by waiting).

    ``timeout_override`` lets long-running paths (LLM proxy) carry a
    larger budget without affecting the default for short ingestion /
    JWKS / image-blob calls.

    ``max_attempts_override`` lets LLM-proxy callers opt out of the
    aggressive retry behaviour. A 300s ReadTimeout on gemma4 never heals
    by waiting 0.5+1+2s and retrying — it just burns 4 × timeout wall
    clock before failing. LLM callers should pass ``max_attempts_override=1``
    (no retry, fail-fast).
    """
    headers = _auth_headers(bearer)
    if extra_headers:
        headers.update(extra_headers)
    timeout = timeout_override if timeout_override is not None else _default_timeout()
    max_attempts = max_attempts_override if max_attempts_override is not None else _MAX_ATTEMPTS

    for attempt in range(max_attempts):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                if stream:
                    req = client.build_request(
                        method, url, headers=headers, json=json_body
                    )
                    response = await client.send(req, stream=True)
                    # Drain inside the client context so the streaming
                    # connection is properly closed regardless of
                    # downstream handling.
                    try:
                        await response.aread()
                    except Exception:
                        await response.aclose()
                        raise
                else:
                    response = await client.request(
                        method, url, headers=headers, json=json_body
                    )

            # ── 5xx → maybe retry ────────────────────────────────
            if response.status_code >= 500:
                if attempt + 1 >= max_attempts:
                    raise CspServerError(
                        f"csp returned {response.status_code} after "
                        f"{attempt + 1} attempts on {method} {url}: "
                        f"{_safe_detail(response)}"
                    )
                backoff = _BACKOFFS[attempt]
                logger.warning(
                    "csp_client retry %d/%d on %s %s after status=%d "
                    "(sleeping %.1fs)",
                    attempt + 1,
                    max_attempts - 1,
                    method,
                    url,
                    response.status_code,
                    backoff,
                )
                await asyncio.sleep(backoff)
                continue

            # ── 4xx → typed error, no retry ──────────────────────
            if response.status_code >= 400:
                _raise_for_4xx(response)

            # ── 2xx/3xx → caller handles ─────────────────────────
            return response

        except (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.RemoteProtocolError,
        ) as exc:
            if attempt + 1 >= max_attempts:
                logger.warning(
                    "csp_client transport error after %d attempts on %s %s: %s",
                    attempt + 1,
                    method,
                    url,
                    exc,
                )
                raise CspServerError(
                    f"csp transport error after {attempt + 1} attempts: {exc}"
                ) from exc
            backoff = _BACKOFFS[attempt]
            logger.warning(
                "csp_client retry %d/%d on %s %s after transport error "
                "(sleeping %.1fs): %s",
                attempt + 1,
                max_attempts - 1,
                method,
                url,
                backoff,
                exc,
            )
            await asyncio.sleep(backoff)
            continue

    # Unreachable: every branch above either returns or raises.
    raise CspServerError(
        f"csp_client exited retry loop without resolution on {method} {url}"
    )


# ── Endpoints ───────────────────────────────────────────────────────────────


async def get_collection(
    collection_id: int, *, bearer: str
) -> CollectionMeta:
    """``GET /api/ingestion/collections/{id}``.

    csp returns the full ``CollectionResponse``; we project to the
    fields studio actually consumes.
    """
    url = f"{settings.CSP_BASE_URL}/api/ingestion/collections/{collection_id}"
    response = await _request("GET", url, bearer=bearer)
    data = response.json()
    return CollectionMeta(
        id=int(data["id"]),
        name=str(data["name"]),
        embedding_model=str(data["embedding_model"]),
        embedding_dim=int(data["embedding_dim"]),
        status=str(data["status"]),
        created_by=int(data["created_by"]),
    )


async def search_chunks(
    collection_id: int,
    query: str,
    *,
    top_k: int = 5,
    min_score: float = 0.0,
    document_ids: list[int] | None = None,
    bearer: str,
) -> list[ChunkHit]:
    """``POST /api/ingestion/collections/{id}/search``.

    Drops the csp envelope (``query`` / ``embedding_model`` /
    ``embedding_dim``) and returns only the hit list. csp's
    ``SearchRequest`` accepts ``document_ids`` as ``list[int] | None``;
    we omit the key entirely when callers pass ``None`` so the wire
    body matches the openapi schema cleanly.
    """
    url = (
        f"{settings.CSP_BASE_URL}"
        f"/api/ingestion/collections/{collection_id}/search"
    )
    body: dict[str, Any] = {
        "query": query,
        "top_k": top_k,
        "min_score": min_score,
    }
    if document_ids is not None:
        body["document_ids"] = list(document_ids)

    response = await _request("POST", url, bearer=bearer, json_body=body)
    payload = response.json()
    return [_chunk_hit_from_dict(r) for r in payload.get("results", [])]


def _chunk_hit_from_dict(d: dict[str, Any]) -> ChunkHit:
    return ChunkHit(
        chunk_id=int(d["chunk_id"]),
        document_id=int(d["document_id"]),
        filename=str(d["filename"]),
        chunk_key=str(d["chunk_key"]),
        content=str(d["content"]),
        score=float(d["score"]),
        metadata=dict(d.get("metadata") or {}),
        parent_chunk_id=(
            int(d["parent_chunk_id"])
            if d.get("parent_chunk_id") is not None
            else None
        ),
        parent_content=(
            str(d["parent_content"])
            if d.get("parent_content") is not None
            else None
        ),
        chunk_type=str(d.get("chunk_type") or "leaf"),
        chunk_level=int(d.get("chunk_level") or 0),
    )


async def search_images(
    collection_id: int,
    query: str,
    *,
    top_k: int = 8,
    min_score: float = 0.0,
    bearer: str,
) -> list[ImageHit]:
    """``POST /api/ingestion/collections/{id}/images/search``.

    Same envelope-stripping pattern as ``search_chunks``.
    """
    url = (
        f"{settings.CSP_BASE_URL}"
        f"/api/ingestion/collections/{collection_id}/images/search"
    )
    body = {
        "query": query,
        "top_k": top_k,
        "min_score": min_score,
    }
    response = await _request("POST", url, bearer=bearer, json_body=body)
    payload = response.json()
    return [_image_hit_from_dict(r) for r in payload.get("results", [])]


def _image_hit_from_dict(d: dict[str, Any]) -> ImageHit:
    return ImageHit(
        image_id=int(d["image_id"]),
        document_id=int(d["document_id"]),
        page=(int(d["page"]) if d.get("page") is not None else None),
        storage_path=str(d["storage_path"]),
        mime=str(d["mime"]),
        caption=(
            str(d["caption"])
            if d.get("caption") is not None
            else None
        ),
        filename=str(d["filename"]),
        score=float(d["score"]),
    )


async def fetch_image_blob(
    image_id: int, *, bearer: str
) -> tuple[bytes, str]:
    """``GET /api/ingestion/images/{id}/blob``.

    Returns ``(raw_bytes, mime_type)``. csp streams the file with
    ``StreamingResponse``; we use httpx's streaming branch on this
    side too so a multi-megabyte raster does not need a synchronous
    ``response.content`` round-trip before the caller decides what to
    do with the bytes.

    csp's ``Content-Type`` header carries the real mime (the table's
    ``mime`` column round-trips through the streaming response). If
    the header is absent we default to ``application/octet-stream``.
    Strip any ``; charset=…`` suffix for binary payloads.
    """
    url = f"{settings.CSP_BASE_URL}/api/ingestion/images/{image_id}/blob"
    response = await _request("GET", url, bearer=bearer, stream=True)
    mime = response.headers.get("content-type", "application/octet-stream")
    if ";" in mime:
        mime = mime.split(";", 1)[0].strip()
    return response.content, mime


async def proxy_chat_completions(
    *,
    model: str,
    messages: list[dict],
    temperature: float | None = None,
    max_tokens: int | None = None,
    response_format: dict | None = None,
    bearer: str,
) -> dict:
    """``POST /v1/chat/completions``.

    csp 的 proxy router 沒額外 prefix (`api_router.include_router(proxy_router)`
    + `proxy.py` 的 `router = APIRouter(tags=...)` 無 prefix),所以實際路徑是
    ``/v1/chat/completions`` 而不是 ``/api/proxy/v1/chat/completions``。

    Returns the full upstream JSON dict so callers can pick the fields
    they want — csp passes through OpenAI's ``choices`` / ``usage``
    payload and tags billing metadata alongside it. The model name is
    forwarded verbatim; do not normalise here (csp performs the
    model_registry lookup itself).

    ``temperature`` / ``max_tokens`` / ``response_format`` default to
    ``None`` and are omitted from the request body when not provided,
    so we don't override OpenAI defaults at the proxy boundary.
    """
    url = f"{settings.CSP_BASE_URL}/v1/chat/completions"
    body: dict[str, Any] = {"model": model, "messages": messages}
    if temperature is not None:
        body["temperature"] = temperature
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    if response_format is not None:
        body["response_format"] = response_format

    response = await _request(
        "POST",
        url,
        bearer=bearer,
        json_body=body,
        # 用量頁要分得出「簡報製作」：csp 把這個來源記到 token_usage.request_type。
        extra_headers={"X-ANILA-Request-Source": "studio"},
        timeout_override=_llm_timeout(),
        # ReadTimeout 不會自己好,retry 4 次只是把總等待時間從 300s 變
        # 1200s 然後一樣失敗。LLM 路徑單次嘗試,失敗就 fail-fast(caller
        # 在 _generate_validated_spec 已有 fallback deck 保底)。
        max_attempts_override=1,
    )
    return response.json()


# 上游 JSON 與解碼後的圖片都設上限，避免把整包回應灌進 CSP、Studio 與渲染器。
MAX_IMAGE_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_DECODED_IMAGE_BYTES = 6 * 1024 * 1024
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"


def _image_mime(raw: bytes) -> str | None:
    if raw.startswith(_PNG_MAGIC):
        return "image/png"
    if raw.startswith(_JPEG_MAGIC):
        return "image/jpeg"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def _decode_generated_image(payload: object) -> tuple[bytes, str] | None:
    import base64

    if not isinstance(payload, dict):
        return None
    try:
        encoded = payload["data"][0]["b64_json"]
    except (KeyError, IndexError, TypeError):
        return None
    if not isinstance(encoded, str):
        return None
    if len(encoded) > MAX_DECODED_IMAGE_BYTES * 2:
        return None
    try:
        raw = base64.b64decode(encoded, validate=False)
    except Exception:
        return None
    if len(raw) > MAX_DECODED_IMAGE_BYTES:
        return None
    mime = _image_mime(raw)
    if mime is None:
        return None
    return raw, mime


async def proxy_image_generation(
    *,
    model: str,
    prompt: str,
    size: str,
    bearer: str,
    task_id: str | None = None,
) -> tuple[bytes, str] | None:
    """``POST /v1/images/generations``。位址是 CSP，不是模型主機。

    提示先收成短的抽象視覺描述。任何失敗都回 None，讓簡報改走沒有生成圖的版面。
    回傳實際圖片位元組與 MIME（png / jpeg / webp）。
    """
    from app.services.image_prompt import outbound_image_prompt

    safe_prompt = outbound_image_prompt(prompt)
    if safe_prompt is None:
        logger.warning("生圖提示不是短的抽象視覺描述，這張投影片不配生成圖片")
        return None

    headers = {"X-ANILA-Request-Source": "studio"}
    if task_id and str(task_id).strip().isdigit():
        headers["X-ANILA-Task-Id"] = str(task_id).strip()

    url = f"{settings.CSP_BASE_URL}/v1/images/generations"
    try:
        response = await _request(
            "POST",
            url,
            bearer=bearer,
            json_body={
                "model": model,
                "prompt": safe_prompt,
                "n": 1,
                "size": size,
                "response_format": "b64_json",
            },
            extra_headers=headers,
            max_attempts_override=1,
        )
    except CspClientError as exc:
        logger.warning("生圖請求失敗，這張投影片不配生成圖片: %s", exc)
        return None
    body = response.content
    if len(body) > MAX_IMAGE_RESPONSE_BYTES:
        logger.warning("生圖回應超過大小上限，這張投影片不配生成圖片")
        return None
    try:
        decoded = _decode_generated_image(json.loads(body))
    except Exception as exc:  # noqa: BLE001 — 上游形狀不對就當沒圖
        logger.warning("生圖回應無法解讀，這張投影片不配生成圖片: %s", exc)
        return None
    if decoded is None:
        logger.warning("生圖回應不是受支援的圖片，這張投影片不配生成圖片")
    return decoded
