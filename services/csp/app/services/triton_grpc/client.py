"""Triton gRPC embedding client (query vs documents) + health probes.

Per-RPC timeouts:
- channel ready / connect: ``CHANNEL_READY_TIMEOUT_S`` = 5s
- ModelInfer: caller-supplied ``timeout_s`` (proxy uses ``EMBEDDING_TIMEOUT``,
  default 30s — measured per-text latency is ~0.02s; 30s is the existing
  OpenAI-path ceiling reused so operators have one knob)
- health (ServerLive / ModelReady / grpc.health.v1): ``HEALTH_TIMEOUT_S`` = 5s

**Whole-call ceilings** (this is the part per-RPC timeouts do not give you).
Both public entry points run on a *shared* default-executor thread via
``asyncio.to_thread``, so their duration is a resource every other
``to_thread`` user in csp competes for. Per-RPC timeouts alone do not bound
that: a call chains several RPCs, and ``embed_texts`` loops once per text, so
a 200-document batch against a peer that accepts TCP and never answers would
have held one thread for ``5 + 200×30`` ≈ 100 minutes with every individual
timeout still being honoured. Each entry point therefore carries one
wall-clock budget covering **every** RPC it makes:

- ``embed_texts``: ``CHANNEL_READY_TIMEOUT_S + timeout_s`` (default **35 s**),
  independent of ``len(texts)``. Each ModelInfer gets
  ``min(timeout_s, remaining)``; an exhausted budget raises rather than
  starting another RPC.
- ``probe_triton_health``: ``CHANNEL_READY_TIMEOUT_S + HEALTH_TIMEOUT_S``
  (**10 s**), covering the ready wait plus the ModelReady → ServerLive →
  grpc.health.v1 fallback chain, which was previously ~20 s with no overall
  deadline.

A ``DEADLINE_EXCEEDED`` from Triton therefore has two different causes, and
``embed_texts`` tells them apart by whether the budget had to shorten that
RPC's deadline: either the upstream did not answer within ``timeout_s`` (the
common single-text failure) or the batch did not fit the budget. At default
settings the first is by far the more likely, so reporting the batch advice
for it — or reporting only ``code=DEADLINE_EXCEEDED``, which names the symptom
and nothing actionable — sends the operator after a problem that is not there.

Above this layer, ``proxy/service.py`` retries ``PROXY_MAX_RETRIES`` (3) times
with exponential backoff, and the backoff ``asyncio.sleep`` releases the
thread. So one HTTP request is bounded at ``3 × 35 + 0.5 + 1.0`` ≈ 106.5 s
wall-clock while never holding a shared thread for more than 35 s at a time.

Channels are lazily created, keyed by ``(host, port, secure)``, guarded by a
threading lock (safe under concurrent first-use). On ``UNAVAILABLE`` RpcError
or channel-ready failure the cached channel is dropped so the next call
rebuilds rather than sticking to a dead socket until process restart.
"""
from __future__ import annotations

import logging
import struct
import threading
import time
from typing import Literal
from urllib.parse import urlparse

import grpc

from app.services.triton_grpc import grpc_service_pb2, grpc_service_pb2_grpc

logger = logging.getLogger(__name__)

EmbedRole = Literal["query", "document"]

CHANNEL_READY_TIMEOUT_S = 5.0
HEALTH_TIMEOUT_S = 5.0
# Whole-probe ceiling for ``probe_triton_health`` — ready wait plus the entire
# ModelReady → ServerLive → grpc.health.v1 fallback chain, not per RPC.
HEALTH_PROBE_BUDGET_S = CHANNEL_READY_TIMEOUT_S + HEALTH_TIMEOUT_S
# Float slack when asking "did the budget clip this RPC's deadline?".
# ``remaining_for`` returns ``min(timeout_s, left)``; equality up to this much
# means the RPC got its own full timeout.
_BUDGET_EPS_S = 1e-6

# Standard gRPC health checking protocol (grpc.health.v1) — this Triton
# answers it; we encode the tiny messages by hand to avoid a second stub set.
_GRPC_HEALTH_SERVICE = "/grpc.health.v1.Health/Check"
# HealthCheckRequest with service="" (field 1 empty string) → tag 0x0a len 0
_GRPC_HEALTH_REQ_EMPTY = b""
# ServingStatus.SERVING == 1
_HEALTH_STATUS_SERVING = 1


class TritonEmbedError(RuntimeError):
    """Upstream Triton call failed; message is safe for logs, not for clients."""


class _Budget:
    """Wall-clock ceiling for one whole client call (see module docstring).

    ``remaining_for(per_rpc)`` is what a single RPC may be given: never more
    than its own timeout, never more than what is left of the budget. It
    raises instead of returning ``<= 0`` so an exhausted budget can never be
    passed to gRPC as a zero/negative deadline.
    """

    __slots__ = ("_deadline", "total")

    def __init__(self, total_s: float) -> None:
        self.total = total_s
        self._deadline = time.monotonic() + total_s

    def remaining(self) -> float:
        return self._deadline - time.monotonic()

    def exhausted(self) -> bool:
        return self.remaining() <= 0

    def remaining_for(self, per_rpc_s: float, *, what: str) -> float:
        left = self.remaining()
        if left <= 0:
            raise TritonEmbedError(
                f"triton call exceeded its {self.total:g}s budget before {what}"
            )
        return min(per_rpc_s, left)


_lock = threading.Lock()
_channels: dict[tuple[str, int, bool], grpc.Channel] = {}


def reset_channel_pool_for_tests() -> None:
    """Close and clear cached channels. Test-only."""
    with _lock:
        for ch in _channels.values():
            try:
                ch.close()
            except Exception:  # pragma: no cover
                pass
        _channels.clear()


def parse_grpc_endpoint(endpoint_url: str) -> tuple[str, int, bool]:
    """Return ``(host, port, secure)`` from a ``grpc://`` / ``grpcs://`` URL.

    Path joining is meaningless for gRPC — only scheme/host/port are used.
    """
    parsed = urlparse((endpoint_url or "").strip())
    if parsed.scheme not in ("grpc", "grpcs"):
        raise TritonEmbedError(
            f"triton endpoint scheme must be grpc:// or grpcs://, got {parsed.scheme!r}"
        )
    host = parsed.hostname
    if not host:
        raise TritonEmbedError("triton endpoint has no hostname")
    if parsed.port is not None:
        port = parsed.port
    else:
        port = 443 if parsed.scheme == "grpcs" else 80
    return host, port, parsed.scheme == "grpcs"


def _get_channel(host: str, port: int, secure: bool) -> grpc.Channel:
    key = (host, port, secure)
    with _lock:
        ch = _channels.get(key)
        if ch is not None:
            return ch
        target = f"{host}:{port}"
        if secure:
            ch = grpc.secure_channel(target, grpc.ssl_channel_credentials())
        else:
            ch = grpc.insecure_channel(target)
        _channels[key] = ch
        return ch


def _drop_channel(host: str, port: int, secure: bool) -> None:
    key = (host, port, secure)
    with _lock:
        ch = _channels.pop(key, None)
    if ch is not None:
        try:
            ch.close()
        except Exception:  # pragma: no cover
            logger.debug("triton channel close failed", exc_info=True)


def _wait_ready(channel: grpc.Channel, budget: _Budget | None = None) -> None:
    wait_s = CHANNEL_READY_TIMEOUT_S
    if budget is not None:
        wait_s = budget.remaining_for(CHANNEL_READY_TIMEOUT_S, what="channel ready")
    future = grpc.channel_ready_future(channel)
    try:
        future.result(timeout=wait_s)
    except Exception as exc:
        raise TritonEmbedError(
            f"triton channel not ready within {wait_s:g}s"
        ) from exc
    finally:
        # Always cancel, success or not. ``channel_ready_future`` registers a
        # connectivity-state callback that stays on the channel otherwise —
        # and these channels are cached and reused for the process lifetime,
        # so one watcher per embed call accumulates. Cancelling a future that
        # already completed is a no-op.
        future.cancel()


def _encode_bytes_tensor(strings: list[str]) -> bytes:
    """Triton BYTES raw contents: repeated (le_u32 length + utf-8 bytes)."""
    out = bytearray()
    for s in strings:
        raw = s.encode("utf-8")
        out.extend(struct.pack("<I", len(raw)))
        out.extend(raw)
    return bytes(out)


def _decode_fp32_matrix(raw: bytes, rows: int, cols: int) -> list[list[float]]:
    n = rows * cols
    expected = n * 4
    if len(raw) != expected:
        raise TritonEmbedError(
            f"triton embeddings byte length {len(raw)} != {rows}*{cols}*4"
        )
    floats = struct.unpack(f"<{n}f", raw)
    return [list(floats[i * cols : (i + 1) * cols]) for i in range(rows)]


def _model_infer(
    stub: grpc_service_pb2_grpc.GRPCInferenceServiceStub,
    *,
    model_name: str,
    input_name: str,
    shape: list[int],
    strings: list[str],
    timeout_s: float,
) -> list[list[float]]:
    request = grpc_service_pb2.ModelInferRequest(
        model_name=model_name,
        inputs=[
            grpc_service_pb2.ModelInferRequest.InferInputTensor(
                name=input_name,
                datatype="BYTES",
                shape=shape,
            )
        ],
        outputs=[
            grpc_service_pb2.ModelInferRequest.InferRequestedOutputTensor(
                name="embeddings"
            )
        ],
        raw_input_contents=[_encode_bytes_tensor(strings)],
    )
    response = stub.ModelInfer(request, timeout=timeout_s)
    if not response.raw_output_contents:
        raise TritonEmbedError("triton ModelInfer returned no raw_output_contents")
    # Output shape from metadata on the tensor, fallback to len(strings)×?
    out_tensor = next(
        (o for o in response.outputs if o.name == "embeddings"),
        None,
    )
    if out_tensor is None or len(out_tensor.shape) != 2:
        raise TritonEmbedError("triton embeddings output missing or bad shape")
    rows, cols = int(out_tensor.shape[0]), int(out_tensor.shape[1])
    return _decode_fp32_matrix(response.raw_output_contents[0], rows, cols)


def embed_texts(
    endpoint_url: str,
    model_name: str,
    texts: list[str],
    *,
    role: EmbedRole,
    timeout_s: float = 30.0,
) -> list[list[float]]:
    """Embed ``texts`` via Triton. ``role`` selects query vs documents input.

    - ``query``: input name ``query``, shape ``[1]`` — **exactly one** string.
    - ``document``: input name ``documents``, shape ``[1, N]`` — one-at-a-time
      here (N=1) because batching was measured at ~5% and is not worth the
      machinery; callers may still pass a list and we loop.

    The whole call — channel wait plus every per-text ModelInfer — is bounded
    by ``CHANNEL_READY_TIMEOUT_S + timeout_s`` (35 s at defaults) regardless
    of ``len(texts)``; see the module docstring for why the per-RPC timeouts
    are not enough on their own.
    """
    if not texts:
        return []
    if not model_name:
        raise TritonEmbedError("triton model_name is required")

    host, port, secure = parse_grpc_endpoint(endpoint_url)
    budget = _Budget(CHANNEL_READY_TIMEOUT_S + timeout_s)

    def _once(text: str) -> list[float]:
        channel = _get_channel(host, port, secure)
        # Which deadline a DEADLINE_EXCEEDED came from is decided here, not in
        # the handler below: ``infer_s < timeout_s`` means the budget clipped
        # this RPC short, i.e. earlier texts ate the call's time.
        infer_s = timeout_s
        try:
            _wait_ready(channel, budget)
            stub = grpc_service_pb2_grpc.GRPCInferenceServiceStub(channel)
            infer_s = budget.remaining_for(timeout_s, what="ModelInfer")
            if role == "query":
                vectors = _model_infer(
                    stub,
                    model_name=model_name,
                    input_name="query",
                    shape=[1],
                    strings=[text],
                    timeout_s=infer_s,
                )
            else:
                vectors = _model_infer(
                    stub,
                    model_name=model_name,
                    input_name="documents",
                    shape=[1, 1],
                    strings=[text],
                    timeout_s=infer_s,
                )
            if not vectors:
                raise TritonEmbedError("triton returned empty embedding batch")
            return vectors[0]
        except TritonEmbedError as exc:
            # Channel-ready failures must drop the cached channel; other
            # TritonEmbedError (e.g. empty batch) leave it alone.
            if "channel not ready" in str(exc):
                _drop_channel(host, port, secure)
            raise
        except grpc.RpcError as exc:
            code = exc.code() if hasattr(exc, "code") else None
            if code == grpc.StatusCode.UNAVAILABLE:
                _drop_channel(host, port, secure)
            if code == grpc.StatusCode.DEADLINE_EXCEEDED:
                if infer_s < timeout_s - _BUDGET_EPS_S:
                    # The deadline that fired was the budget's, not this RPC's
                    # own timeout: earlier texts in this batch already spent
                    # the call's time. Saying DEADLINE_EXCEEDED here would send
                    # an operator hunting a slow upstream when the actual
                    # answer is "this batch is too big for one call".
                    raise TritonEmbedError(
                        f"triton call exceeded its {budget.total:g}s budget "
                        f"({len(texts)} 段文字未在時限內完成;請縮小批次或調高 "
                        f"EMBEDDING_TIMEOUT)"
                    ) from exc
                # This RPC got its full ``timeout_s`` and the upstream still
                # did not answer. That is the common single-text failure, and
                # the batch advice above would be actively misleading for it —
                # ``code=DEADLINE_EXCEEDED`` alone was too, since it named the
                # symptom and not one thing the operator can do.
                raise TritonEmbedError(
                    f"triton 未在 {infer_s:g}s 內回應 ModelInfer —— 上游過慢或"
                    f"該 model 未載入;確認 Triton 上的 model name 與 "
                    f"EMBEDDING_TIMEOUT"
                ) from exc
            raise TritonEmbedError(
                f"triton RpcError code={getattr(code, 'name', code)}"
            ) from exc

    # One-at-a-time by design (measured ~5% batch gain — not worth it).
    return [_once(t) for t in texts]


def _grpc_health_v1_serving(
    channel: grpc.Channel, budget: _Budget | None = None
) -> bool:
    """True when ``grpc.health.v1.Health/Check`` reports SERVING."""
    try:
        timeout_s = HEALTH_TIMEOUT_S
        if budget is not None:
            timeout_s = budget.remaining_for(HEALTH_TIMEOUT_S, what="health.v1 Check")
        check = channel.unary_unary(
            _GRPC_HEALTH_SERVICE,
            request_serializer=lambda _r: _GRPC_HEALTH_REQ_EMPTY,
            response_deserializer=lambda b: b,
        )
        raw = check(None, timeout=timeout_s)
    except TritonEmbedError:
        # Budget exhausted — no time left to ask, so no evidence of SERVING.
        return False
    except grpc.RpcError:
        return False
    # HealthCheckResponse: field 1 varint status. SERVING=1 → b"\x08\x01"
    if not raw:
        return False
    # Minimal decode: look for tag 1 varint == SERVING.
    i = 0
    while i < len(raw):
        tag = raw[i]
        i += 1
        field = tag >> 3
        wire = tag & 7
        if wire == 0:  # varint
            value = 0
            shift = 0
            while i < len(raw):
                b = raw[i]
                i += 1
                value |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            if field == 1:
                return value == _HEALTH_STATUS_SERVING
        else:
            return False
    return False


def probe_triton_health(
    endpoint_url: str,
    *,
    model_name: str | None = None,
) -> tuple[str, int]:
    """Return ``(five_state_status, latency_ms)`` for a Triton endpoint.

    Status strings match ``health_checker`` five-state vocabulary
    (``healthy`` / ``degraded`` / ``unhealthy``) without importing that
    module — avoids an import cycle with the health sweep.

    When ``model_name`` is set, ``ModelReady`` is authoritative: ready →
    healthy, not-ready → unhealthy (do not greenwash via ServerLive).
    Without a model name, fall back to ServerLive / grpc.health.v1.

    The whole probe — ready wait plus the ModelReady → ServerLive →
    grpc.health.v1 fallback chain — shares one
    ``HEALTH_PROBE_BUDGET_S`` (10 s) deadline. Each of those RPCs used to
    carry its own independent 5 s timeout, so a peer that accepts TCP and
    answers nothing could keep the sweep on one shared executor thread for
    ~20 s. The sweep runs on a timer over every registered model; an
    unbounded chain there is how one dead endpoint slows the whole
    dashboard.
    """
    started = time.monotonic()
    budget = _Budget(HEALTH_PROBE_BUDGET_S)

    def _elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    try:
        host, port, secure = parse_grpc_endpoint(endpoint_url)
    except TritonEmbedError:
        return "unhealthy", _elapsed()

    channel = _get_channel(host, port, secure)
    try:
        _wait_ready(channel, budget)
    except TritonEmbedError:
        _drop_channel(host, port, secure)
        return "unhealthy", _elapsed()

    stub = grpc_service_pb2_grpc.GRPCInferenceServiceStub(channel)
    try:
        if model_name:
            ready = stub.ModelReady(
                grpc_service_pb2.ModelReadyRequest(name=model_name),
                timeout=budget.remaining_for(HEALTH_TIMEOUT_S, what="ModelReady"),
            )
            # ModelReady answered — its boolean is the truth for this model.
            # Falling through to ServerLive would paint a false green when the
            # server is up but the model is not loaded.
            return ("healthy" if ready.ready else "unhealthy"), _elapsed()
        live = stub.ServerLive(
            grpc_service_pb2.ServerLiveRequest(),
            timeout=budget.remaining_for(HEALTH_TIMEOUT_S, what="ServerLive"),
        )
        if live.live:
            return "healthy", _elapsed()
        if _grpc_health_v1_serving(channel, budget):
            return "healthy", _elapsed()
        return "degraded", _elapsed()
    except TritonEmbedError:
        # Budget exhausted mid-chain — a probe that ran out of time is not a
        # healthy endpoint, and must not start another RPC to find out.
        return "unhealthy", _elapsed()
    except grpc.RpcError as exc:
        code = exc.code() if hasattr(exc, "code") else None
        if code == grpc.StatusCode.DEADLINE_EXCEEDED:
            return "degraded", _elapsed()
        if code == grpc.StatusCode.UNAVAILABLE:
            _drop_channel(host, port, secure)
            return "unhealthy", _elapsed()
        # ModelReady RPC failed for another reason: try server-level signals
        # only when we were asking about a specific model (degraded, not green
        # on server-live alone — operators still see something is wrong).
        # Still inside the same budget: the fallback cannot extend the ceiling.
        if model_name:
            try:
                live = stub.ServerLive(
                    grpc_service_pb2.ServerLiveRequest(),
                    timeout=budget.remaining_for(
                        HEALTH_TIMEOUT_S, what="ServerLive fallback"
                    ),
                )
                if live.live or _grpc_health_v1_serving(channel, budget):
                    return "degraded", _elapsed()
            except Exception:
                pass
        return "unhealthy", _elapsed()
    except Exception:
        logger.debug("triton health probe failed", exc_info=True)
        return "unhealthy", _elapsed()