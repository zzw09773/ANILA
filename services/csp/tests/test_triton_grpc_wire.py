"""What actually goes onto the gRPC wire — and how long a call may hold a thread.

Why this file exists
--------------------
Every other Triton test replaces ``embed_texts`` with a fake (``embed_texts``
appears in ``services/csp/tests/`` only inside ``monkeypatch.setattr``), so the
one thing the whole ``triton_grpc`` package was built for — mapping
``role='query'`` to a **different input tensor** than ``role='document'`` —
had no test at all. A mutation that made the query branch send
``input_name="documents", shape=[1, 1]`` left the full suite byte-identical
while, against the real Triton, ``cosine(query, document)`` went from 0.828 to
1.0: the query and the document became the same vector and retrieval ranking
quietly collapsed. Two things that looked like guards were
``inspect.getsource()`` string greps, which only catch a literal edit at one
call site and would not have moved.

So these tests speak real gRPC to a real in-process server and assert on the
``ModelInferRequest`` protobuf it receives. ``import grpc`` is deliberately
unconditional rather than ``importorskip``: ``grpcio`` is a pinned production
dependency of csp (``requirements.txt``), so an environment without it is a
broken environment and should say so loudly instead of skipping the only
coverage this behaviour has.

The stall tests below are also the measurement behind the whole-call ceilings
documented in ``triton_grpc/client.py`` — see ``_Budget``.
"""
from __future__ import annotations

import socket
import struct
import threading
import time
from concurrent import futures

import grpc
import pytest

from app.services.triton_grpc import client as triton_client
from app.services.triton_grpc import grpc_service_pb2, grpc_service_pb2_grpc

_DIM = 4
# Two orthogonal unit vectors. The server picks one purely from the input
# tensor NAME it receives, so "which tensor did we send" becomes observable in
# the returned vector — the same signal the live cosine check reads.
_QUERY_VEC = (1.0, 0.0, 0.0, 0.0)
_DOCUMENT_VEC = (0.0, 1.0, 0.0, 0.0)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb + 1e-12)


def _decode_bytes_tensor(raw: bytes) -> list[str]:
    """Inverse of client._encode_bytes_tensor — read the strings off the wire."""
    out: list[str] = []
    i = 0
    while i < len(raw):
        (length,) = struct.unpack_from("<I", raw, i)
        i += 4
        out.append(raw[i : i + length].decode("utf-8"))
        i += length
    return out


class _RecordingTriton(grpc_service_pb2_grpc.GRPCInferenceServiceServicer):
    """Records every ModelInferRequest and answers by input tensor name."""

    def __init__(self, infer_delay_s: float = 0.0) -> None:
        self.requests: list = []
        self._lock = threading.Lock()
        self._infer_delay_s = infer_delay_s

    def ModelInfer(self, request, context):
        if self._infer_delay_s:
            time.sleep(self._infer_delay_s)
        with self._lock:
            self.requests.append(request)
        name = request.inputs[0].name if request.inputs else ""
        vec = _QUERY_VEC if name == "query" else _DOCUMENT_VEC
        return grpc_service_pb2.ModelInferResponse(
            model_name=request.model_name,
            outputs=[
                grpc_service_pb2.ModelInferResponse.InferOutputTensor(
                    name="embeddings",
                    datatype="FP32",
                    shape=[1, _DIM],
                )
            ],
            raw_output_contents=[struct.pack(f"<{_DIM}f", *vec)],
        )


class _StallingTriton(grpc_service_pb2_grpc.GRPCInferenceServiceServicer):
    """Completes the handshake, then never answers ServerLive / health.

    ``ModelReady`` fails fast with a code that is neither DEADLINE_EXCEEDED nor
    UNAVAILABLE, which is exactly what sends ``probe_triton_health`` down its
    ServerLive → grpc.health.v1 fallback chain.
    """

    def __init__(self, model_ready_delay_s: float) -> None:
        self._model_ready_delay_s = model_ready_delay_s
        self.never = threading.Event()

    def ModelReady(self, request, context):
        time.sleep(self._model_ready_delay_s)
        context.abort(grpc.StatusCode.INTERNAL, "model repository unreadable")

    def ServerLive(self, request, context):
        self.never.wait()  # released in the fixture teardown
        return grpc_service_pb2.ServerLiveResponse(live=True)


def _serve(servicer):
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    grpc_service_pb2_grpc.add_GRPCInferenceServiceServicer_to_server(
        servicer, server
    )
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    return server, f"grpc://127.0.0.1:{port}"


@pytest.fixture
def triton():
    """Recording Triton on a loopback port; channel pool cleared either side."""
    triton_client.reset_channel_pool_for_tests()
    servicer = _RecordingTriton()
    server, url = _serve(servicer)
    try:
        yield servicer, url
    finally:
        # Order matters: drain the server first, then close the client
        # channels. Closing a channel while grpc's own connectivity-poller
        # thread is mid-check raises in that thread, which pytest surfaces as
        # PytestUnhandledThreadExceptionWarning and makes the real result hard
        # to read. Not a defect in the code under test.
        server.stop(None).wait(2.0)
        time.sleep(0.05)
        triton_client.reset_channel_pool_for_tests()


# ── the query/document split, on the wire ────────────────────────────────────


def test_query_role_sends_the_query_tensor(triton):
    servicer, url = triton
    triton_client.embed_texts(url, "nv-embed-v2", ["找出去年的採購紀錄"], role="query")

    assert len(servicer.requests) == 1
    req = servicer.requests[0]
    assert req.model_name == "nv-embed-v2"
    assert len(req.inputs) == 1
    tensor = req.inputs[0]
    assert tensor.name == "query"
    assert list(tensor.shape) == [1]
    assert tensor.datatype == "BYTES"
    assert _decode_bytes_tensor(req.raw_input_contents[0]) == ["找出去年的採購紀錄"]


def test_document_role_sends_the_documents_tensor(triton):
    servicer, url = triton
    triton_client.embed_texts(url, "nv-embed-v2", ["採購紀錄全文"], role="document")

    req = servicer.requests[0]
    tensor = req.inputs[0]
    assert tensor.name == "documents"
    assert list(tensor.shape) == [1, 1]
    assert tensor.datatype == "BYTES"
    assert _decode_bytes_tensor(req.raw_input_contents[0]) == ["採購紀錄全文"]


def test_query_and_document_do_not_collapse_to_one_vector(triton):
    """The live-cosine invariant, made deterministic.

    Same text, both roles. The server answers purely from the input tensor
    name, so if the query branch ever starts sending the documents tensor the
    two vectors become identical and the cosine goes to 1.0 — the exact silent
    degradation measured against the real Triton.
    """
    _servicer, url = triton
    text = "同一段文字"
    q = triton_client.embed_texts(url, "nv-embed-v2", [text], role="query")[0]
    d = triton_client.embed_texts(url, "nv-embed-v2", [text], role="document")[0]

    assert q != d
    assert _cosine(q, d) < 0.99, (
        "query 與 document 端算出同一個向量 —— 查詢被當成文件編碼了"
    )


def test_every_text_in_a_batch_keeps_its_role(triton):
    """The per-text loop must not drift to the other tensor partway through."""
    servicer, url = triton
    triton_client.embed_texts(
        url, "nv-embed-v2", ["甲", "乙", "丙"], role="document"
    )
    assert [r.inputs[0].name for r in servicer.requests] == ["documents"] * 3
    assert [list(r.inputs[0].shape) for r in servicer.requests] == [[1, 1]] * 3


def test_role_is_not_derived_from_batch_size(triton):
    """A one-element document batch is still documents, not query.

    Guards the tempting "len(texts) == 1 → it must be a query" shortcut.
    """
    servicer, url = triton
    triton_client.embed_texts(url, "nv-embed-v2", ["只有一段"], role="document")
    assert servicer.requests[0].inputs[0].name == "documents"


# ── I7: how long one call may hold a shared executor thread ──────────────────


def test_embed_texts_budget_bounds_a_slow_multi_text_batch(monkeypatch):
    """A big batch against a slow peer must not hold a thread per-text.

    ``embed_texts`` runs on the shared ``asyncio.to_thread`` default executor,
    and it loops once per text. With only per-RPC timeouts, 100 texts × a 30 s
    ModelInfer ceiling is ~50 minutes on one shared thread with every
    individual timeout honoured. The whole call carries one budget of
    ``CHANNEL_READY_TIMEOUT_S + timeout_s`` instead.

    Scaled down here (ready 0.5 s + timeout 0.5 s = 1.0 s budget) against a
    server that takes 0.1 s per infer: unbudgeted this is ~10 s.
    """
    monkeypatch.setattr(triton_client, "CHANNEL_READY_TIMEOUT_S", 0.5)
    triton_client.reset_channel_pool_for_tests()
    servicer = _RecordingTriton(infer_delay_s=0.1)
    server, url = _serve(servicer)
    try:
        started = time.monotonic()
        with pytest.raises(triton_client.TritonEmbedError) as exc:
            triton_client.embed_texts(
                url,
                "nv-embed-v2",
                [f"文件 {i}" for i in range(100)],
                role="document",
                timeout_s=0.5,
            )
        elapsed = time.monotonic() - started
    finally:
        # Stop the server and let the last deadline-exceeded RPC unwind before
        # closing the client channel: closing it out from under grpc's own
        # connectivity-poller thread raises there, which pytest reports as an
        # unhandled thread exception and buries the real result.
        server.stop(None).wait(2.0)
        time.sleep(0.1)
        triton_client.reset_channel_pool_for_tests()

    assert "budget" in str(exc.value)
    # Ceiling is 1.0 s; allow generous slack for scheduling, but this must be
    # nowhere near the ~10 s an unbudgeted loop would take.
    assert elapsed < 4.0, f"embed_texts held the thread for {elapsed:.1f}s"
    # It really did do useful work before running out, i.e. the budget is not
    # just failing on the first call.
    assert len(servicer.requests) >= 2


def test_health_probe_budget_bounds_the_fallback_chain(monkeypatch):
    """ModelReady → ServerLive → health.v1 shares one deadline.

    Each of those RPCs used to carry its own independent timeout, so one peer
    that accepts TCP and answers nothing kept the health sweep on a shared
    thread for the sum of them. Scaled down: ModelReady fails after 1.0 s,
    ServerLive never answers. Per-RPC only → 1.0 + 3.0 = ~4.0 s. Budgeted at
    1.5 s → ServerLive gets the 0.5 s that is left.
    """
    monkeypatch.setattr(triton_client, "CHANNEL_READY_TIMEOUT_S", 1.0)
    monkeypatch.setattr(triton_client, "HEALTH_TIMEOUT_S", 3.0)
    monkeypatch.setattr(triton_client, "HEALTH_PROBE_BUDGET_S", 1.5)
    triton_client.reset_channel_pool_for_tests()
    servicer = _StallingTriton(model_ready_delay_s=1.0)
    server, url = _serve(servicer)
    try:
        started = time.monotonic()
        status, _ms = triton_client.probe_triton_health(
            url, model_name="nv-embed-v2"
        )
        elapsed = time.monotonic() - started
    finally:
        servicer.never.set()
        triton_client.reset_channel_pool_for_tests()
        server.stop(None)

    assert status == "unhealthy"
    assert elapsed < 2.5, f"health probe chain ran {elapsed:.1f}s past its budget"


def test_unreachable_peer_is_bounded_by_the_channel_ready_wait():
    """A peer that accepts TCP and never speaks gRPC fails at the ready wait.

    Bounded by ``CHANNEL_READY_TIMEOUT_S`` alone — asserted here so that
    shortening the handshake wait can never silently become unbounded.
    """
    black_hole = socket.socket()
    black_hole.bind(("127.0.0.1", 0))
    black_hole.listen(8)
    port = black_hole.getsockname()[1]
    triton_client.reset_channel_pool_for_tests()
    try:
        started = time.monotonic()
        with pytest.raises(triton_client.TritonEmbedError) as exc:
            triton_client.embed_texts(
                f"grpc://127.0.0.1:{port}",
                "nv-embed-v2",
                ["x"],
                role="query",
                timeout_s=30.0,
            )
        elapsed = time.monotonic() - started
    finally:
        triton_client.reset_channel_pool_for_tests()
        black_hole.close()

    assert "channel not ready" in str(exc.value)
    assert elapsed < triton_client.CHANNEL_READY_TIMEOUT_S + 2.0
