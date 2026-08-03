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

import inspect
import ipaddress
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


class MalformedBytesTensor(AssertionError):
    """The payload is not valid Triton BYTES raw contents."""


def decode_bytes_tensor(raw: bytes) -> list[str]:
    """Inverse of client._encode_bytes_tensor — and it is allowed to FAIL.

    The previous version sliced: ``raw[i : i + length]`` returns whatever is
    there and never complains, so it round-tripped a one-element tensor no
    matter what the length prefix said. That is why flipping the prefix to
    big-endian left every payload assertion in this file and in
    ``test_triton_grpc_tensor_contract.py`` green while, against the real
    Triton, every embed call died with ``INVALID_ARGUMENT`` (measured
    2026-08-03 against 172.16.120.35:9001).

    A decoder that cannot fail is not a check. This one refuses a length
    prefix that does not fit what is left, which is exactly what a big-endian
    27 looks like when read little-endian (452984832).
    """
    out: list[str] = []
    i = 0
    while i < len(raw):
        if len(raw) - i < 4:
            raise MalformedBytesTensor(
                f"BYTES tensor 在 offset {i} 只剩 {len(raw) - i} bytes,不夠一個長度前綴"
            )
        (length,) = struct.unpack_from("<I", raw, i)
        i += 4
        if length > len(raw) - i:
            raise MalformedBytesTensor(
                f"BYTES tensor 在 offset {i - 4} 宣告長度 {length},"
                f"但只剩 {len(raw) - i} bytes —— payload 不是 little-endian 長度前綴?"
            )
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


# ── the BYTES length prefix: little-endian, pinned byte for byte ─────────────
#
# ``struct.pack("<I", …)`` → ``">I"`` in ``_encode_bytes_tensor`` is one
# character, and it kills **every** embedding call on the Triton path. Measured
# 2026-08-03 against the live Triton at 172.16.120.35:9001:
#
#     little-endian (shipped) → dim 4096, cosine(query, document) 0.7467
#     big-endian              → TritonEmbedError: triton RpcError
#                               code=INVALID_ARGUMENT
#
# and the full suite stayed at 1892 passed / 13 skipped / **0 failed**, byte
# identical to the clean baseline. Every payload assertion in this file and in
# ``test_triton_grpc_tensor_contract.py`` sent exactly **one** string, and the
# decoder above used to slice instead of check, so one element round-tripped
# whatever the prefix said. (Two strings do not: with a slicing decoder the
# big-endian payload came back as ``'找出去年的採購紀錄\x00\x00\x00\t第二段'``.)
#
# The tests below therefore compare raw bytes against literals — nothing here
# is derived from ``struct``, so they cannot follow the implementation if it
# moves. With them and the strict decoder in place the same mutation is
# **12 failed / 1905 passed / 13 skipped** on the full suite (re-measured
# 2026-08-03): 8 here and 4 in the tensor-contract file.


def test_the_length_prefix_of_one_string_is_little_endian():
    text = "找出去年的採購紀錄"  # 9 CJK chars = 27 utf-8 bytes = 0x1b
    assert triton_client._encode_bytes_tensor([text]) == (
        b"\x1b\x00\x00\x00" + text.encode("utf-8")
    )


def test_a_multi_element_bytes_tensor_is_pinned_byte_for_byte():
    """Two strings in one tensor — the case a single string cannot show.

    ``"甲"`` is 3 utf-8 bytes (E7 94 B2), ``"ab"`` is 2. Confirmed identical to
    what the shipped encoder produced when the live acceptance ran.
    """
    assert triton_client._encode_bytes_tensor(["ab", "甲"]) == (
        b"\x02\x00\x00\x00ab\x03\x00\x00\x00\xe7\x94\xb2"
    )


def test_the_encoder_round_trips_a_multi_element_tensor():
    texts = ["找出去年的採購紀錄", "第二段", "", "x"]
    assert decode_bytes_tensor(triton_client._encode_bytes_tensor(texts)) == texts


def test_the_decoder_refuses_a_big_endian_payload():
    """The decoder must be able to fail, or it papers over exactly this.

    Big-endian 27 read little-endian is 452984832 — a length that cannot fit
    in what is left. The old slicing decoder happily returned one correct
    string from this payload, which is how the mutation stayed invisible.
    """
    text = "找出去年的採購紀錄"
    raw = text.encode("utf-8")
    big_endian = struct.pack(">I", len(raw)) + raw

    with pytest.raises(MalformedBytesTensor):
        decode_bytes_tensor(big_endian)


def test_the_decoder_refuses_a_truncated_payload():
    good = triton_client._encode_bytes_tensor(["找出去年的採購紀錄"])

    with pytest.raises(MalformedBytesTensor):
        decode_bytes_tensor(good[:-3])
    with pytest.raises(MalformedBytesTensor):
        decode_bytes_tensor(good + b"\x05\x00")


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
    assert decode_bytes_tensor(req.raw_input_contents[0]) == ["找出去年的採購紀錄"]


def test_document_role_sends_the_documents_tensor(triton):
    servicer, url = triton
    triton_client.embed_texts(url, "nv-embed-v2", ["採購紀錄全文"], role="document")

    req = servicer.requests[0]
    tensor = req.inputs[0]
    assert tensor.name == "documents"
    assert list(tensor.shape) == [1, 1]
    assert tensor.datatype == "BYTES"
    assert decode_bytes_tensor(req.raw_input_contents[0]) == ["採購紀錄全文"]


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


# ── the documented default ceilings, not just the mechanism ──────────────────
#
# The stall tests above monkeypatch every constant they exercise, so they pin
# how the budget WORKS and say nothing about the numbers the module docstring
# and the runbook promise — a shared executor thread could be held nearly four
# times as long with the suite still green. The two tests below close that: the
# first pins the documented numbers, the second pins the budget the calls
# actually construct.
#
# Both mutations re-measured on the FULL suite, 2026-08-03 (the round-3 report
# quoted 1 failure for the second one; it is 2 — under-reporting your own
# coverage is the mirror of over-claiming it):
#
#   HEALTH_PROBE_BUDGET_S = 600.0                      → 2 failed / 1915 passed
#     test_default_ceilings_are_the_documented_ones
#     test_the_budget_a_call_actually_constructs
#   _Budget(CHANNEL_READY_TIMEOUT_S + timeout_s * 4)   → 2 failed / 1915 passed
#     test_the_budget_a_call_actually_constructs
#     test_an_oversized_batch_still_says_shrink_the_batch


def test_default_ceilings_are_the_documented_ones():
    """35 s per embed call, 10 s per health probe — at default settings.

    These exact numbers are quoted in the module docstring, in the runbook's
    §3.1c troubleshooting table (「單次請求的執行緒佔用上限為 35 秒」) and in
    the retry arithmetic (3 × 35 ≈ 106.5 s per HTTP request). Changing them is
    allowed; changing them without noticing is not.
    """
    assert triton_client.CHANNEL_READY_TIMEOUT_S == 5.0
    assert triton_client.HEALTH_TIMEOUT_S == 5.0
    assert triton_client.HEALTH_PROBE_BUDGET_S == 10.0
    # The advertised 35 s is 5 s handshake + the default per-RPC timeout, so
    # that default is part of the promise too.
    default_timeout = inspect.signature(
        triton_client.embed_texts
    ).parameters["timeout_s"].default
    assert default_timeout == 30.0
    assert triton_client.CHANNEL_READY_TIMEOUT_S + default_timeout == 35.0


def test_the_budget_a_call_actually_constructs(triton, monkeypatch):
    """Not the constant — the ceiling ``embed_texts`` / ``probe_triton_health``
    hand to ``_Budget`` on a real call.

    A constant can stay 5.0 while the call multiplies it. Recording the
    construction is what makes ``_Budget(CHANNEL_READY_TIMEOUT_S + timeout_s
    * 4)`` visible.
    """
    _servicer, url = triton
    totals: list[float] = []

    class _RecordingBudget(triton_client._Budget):
        def __init__(self, total_s: float) -> None:
            totals.append(total_s)
            super().__init__(total_s)

    monkeypatch.setattr(triton_client, "_Budget", _RecordingBudget)

    triton_client.embed_texts(url, "nv-embed-v2", ["一段文字"], role="document")
    assert totals == [35.0], "embed_texts 的整通呼叫上限不是文件寫的 35 秒"

    # Scales with the caller's per-RPC timeout, and with nothing else — in
    # particular not with len(texts).
    totals.clear()
    triton_client.embed_texts(
        url, "nv-embed-v2", ["甲", "乙", "丙"], role="document", timeout_s=1.0
    )
    assert totals == [6.0]

    totals.clear()
    triton_client.probe_triton_health(url, model_name="nv-embed-v2")
    assert totals == [10.0], "health probe 的整通呼叫上限不是文件寫的 10 秒"


# ── the connectivity watcher must not accumulate on a pooled channel ─────────


class _CallbackCountingChannel:
    """Just enough channel for ``grpc.channel_ready_future`` to run on.

    ``channel_ready_future`` works by ``subscribe``-ing a connectivity-state
    callback and ``unsubscribe``-ing it again when it matures or is cancelled.
    Channels here are pooled for the process lifetime, so a callback left
    behind on the failure path is a per-call leak on a long-lived object —
    invisible to every other test, which is why removing ``future.cancel()``
    from ``_wait_ready`` left 26 of them passing.
    """

    def __init__(self, *, becomes_ready: bool) -> None:
        self.callbacks: list = []
        self._becomes_ready = becomes_ready

    def subscribe(self, callback, try_to_connect=False):  # noqa: ARG002
        self.callbacks.append(callback)
        if self._becomes_ready:
            callback(grpc.ChannelConnectivity.READY)

    def unsubscribe(self, callback):
        self.callbacks.remove(callback)


def test_wait_ready_leaves_no_watcher_behind_when_it_times_out():
    """The failure path is the leaking one — nothing matures, so only the
    explicit cancel takes the callback off the channel."""
    channel = _CallbackCountingChannel(becomes_ready=False)

    with pytest.raises(triton_client.TritonEmbedError):
        triton_client._wait_ready(channel, triton_client._Budget(0.1))

    assert channel.callbacks == [], (
        "channel_ready_future 的連線狀態 callback 沒被取消 —— "
        "channel 是整個 process 共用的,每次 embed 失敗就多留一個"
    )


def test_wait_ready_leaves_no_watcher_behind_on_success():
    channel = _CallbackCountingChannel(becomes_ready=True)

    triton_client._wait_ready(channel, triton_client._Budget(5.0))

    assert channel.callbacks == []


def test_repeated_failed_waits_do_not_pile_up_on_one_pooled_channel():
    """The shape of the leak as an operator would hit it: one long-lived
    channel, many failed embed calls."""
    channel = _CallbackCountingChannel(becomes_ready=False)

    for _ in range(10):
        with pytest.raises(triton_client.TritonEmbedError):
            triton_client._wait_ready(channel, triton_client._Budget(0.02))

    assert channel.callbacks == []


# ── which deadline fired: the RPC's own, or the whole-call budget ────────────


def test_a_slow_upstream_is_not_reported_as_an_oversized_batch(monkeypatch):
    """One text, upstream too slow → say so; do not advise shrinking a batch.

    The budget is 35 s at defaults and one ModelInfer is 30 s, so on the most
    common failure — a single embed against a peer that has stopped answering
    — the budget is never exhausted and the operator used to get
    ``triton RpcError code=DEADLINE_EXCEEDED``: the symptom, with nothing to
    act on. The batch advice would be worse than useless here; there is no
    batch.
    """
    monkeypatch.setattr(triton_client, "CHANNEL_READY_TIMEOUT_S", 0.5)
    triton_client.reset_channel_pool_for_tests()
    servicer = _RecordingTriton(infer_delay_s=1.0)
    server, url = _serve(servicer)
    try:
        with pytest.raises(triton_client.TritonEmbedError) as exc:
            triton_client.embed_texts(
                url, "nv-embed-v2", ["一段文字"], role="query", timeout_s=0.3
            )
    finally:
        server.stop(None).wait(2.0)
        time.sleep(0.1)
        triton_client.reset_channel_pool_for_tests()

    message = str(exc.value)
    assert "0.3s" in message and "ModelInfer" in message
    assert "縮小批次" not in message, "單筆逾時被講成批次太大"
    assert "DEADLINE_EXCEEDED" not in message


def test_an_oversized_batch_still_says_shrink_the_batch(monkeypatch):
    """The other side of the same fork: the budget clipped this RPC short.

    Every infer (0.3 s) fits inside its own 0.5 s timeout, so no single text is
    slow — it is the batch that does not fit the 0.7 s budget, and the RPC that
    finally fails does so on a deadline the budget shortened.
    """
    monkeypatch.setattr(triton_client, "CHANNEL_READY_TIMEOUT_S", 0.2)
    triton_client.reset_channel_pool_for_tests()
    servicer = _RecordingTriton(infer_delay_s=0.3)
    server, url = _serve(servicer)
    try:
        with pytest.raises(triton_client.TritonEmbedError) as exc:
            triton_client.embed_texts(
                url,
                "nv-embed-v2",
                [f"文件 {i}" for i in range(5)],
                role="document",
                timeout_s=0.5,
            )
    finally:
        server.stop(None).wait(2.0)
        time.sleep(0.1)
        triton_client.reset_channel_pool_for_tests()

    message = str(exc.value)
    assert "budget" in message
    assert "縮小批次" in message


# ── the whole chain: HTTP body → proxy → gRPC input tensor ───────────────────
#
# ``test_embeddings_input_type.py`` proves the route hands the declared role to
# the Triton client; the tests at the top of this file prove the client turns a
# role into a tensor name. This one refuses to take the join on trust: a real
# POST to ``/v1/embeddings`` and the ``ModelInferRequest`` a real gRPC server
# receives, with nothing faked in between.
#
# The URL guard rejects loopback for grpc:// unconditionally (and must keep
# doing so), so the server binds to a non-loopback RFC1918 address on this
# host and the endpoint is registered with the private-IP flag set — exactly
# the on-prem posture the runbook describes.


def _private_ipv4() -> str | None:
    """A non-loopback RFC1918 address this host can bind and reach."""
    candidates: list[str] = []
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("10.255.255.255", 1))  # no packet leaves the host
        candidates.append(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            candidates.append(info[4][0])
    except socket.gaierror:
        pass
    for addr in candidates:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if ip.is_private and not (ip.is_loopback or ip.is_link_local):
            return addr
    return None


_LAN_IPV4 = _private_ipv4()


@pytest.mark.skipif(
    _LAN_IPV4 is None,
    reason="no non-loopback RFC1918 address to bind (loopback is refused by the URL guard, correctly)",
)
@pytest.mark.parametrize(
    "declared,expected_tensor,expected_shape",
    [("query", "query", [1]), ("document", "documents", [1, 1]), (None, "documents", [1, 1])],
)
def test_http_input_type_reaches_the_gRPC_input_tensor(
    client, db, monkeypatch, declared, expected_tensor, expected_shape
):
    from app.models.model_registry import ModelRegistry
    from app.services.auth_service import create_tokens

    from tests.conftest import make_user

    monkeypatch.setenv("ANILA_ALLOW_GRPC_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_ALLOW_PRIVATE_ENDPOINT", "1")

    triton_client.reset_channel_pool_for_tests()
    servicer = _RecordingTriton()
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    grpc_service_pb2_grpc.add_GRPCInferenceServiceServicer_to_server(
        servicer, server
    )
    port = server.add_insecure_port(f"{_LAN_IPV4}:0")
    server.start()
    try:
        model = ModelRegistry(
            name="nv-embed-v2",
            display_name="nv-embed-v2",
            model_type="embedding",
            endpoint_url=f"grpc://{_LAN_IPV4}:{port}",
            api_version="v1",
            protocol="triton_grpc",
            is_active=True,
        )
        db.add(model)
        db.commit()
        user = make_user(db, username="wire_caller", role="admin")

        body = {"model": "nv-embed-v2", "input": "找出去年的採購紀錄"}
        if declared is not None:
            body["input_type"] = declared

        resp = client.post(
            "/v1/embeddings",
            json=body,
            headers={
                "Authorization": f"Bearer {create_tokens(user)['access_token']}"
            },
        )
    finally:
        server.stop(None).wait(2.0)
        time.sleep(0.05)
        triton_client.reset_channel_pool_for_tests()

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/json")

    assert len(servicer.requests) == 1
    tensor = servicer.requests[0].inputs[0]
    assert tensor.name == expected_tensor
    assert list(tensor.shape) == expected_shape
    assert decode_bytes_tensor(
        servicer.requests[0].raw_input_contents[0]
    ) == ["找出去年的採購紀錄"]
    # 連位元組本身都釘住 —— 這條鏈是唯一沒有任何替身的那一條,所以「線上真的
    # 長這樣」在這裡講最有份量:長度前綴的位元序一動就變紅。
    assert servicer.requests[0].raw_input_contents[0] == (
        b"\x1b\x00\x00\x00" + "找出去年的採購紀錄".encode("utf-8")
    )

    # And the vector that came back is the one that tensor selects, so a
    # query-shaped request can never be answered by the documents branch.
    returned = resp.json()["data"][0]["embedding"]
    expected_vec = list(_QUERY_VEC if expected_tensor == "query" else _DOCUMENT_VEC)
    assert [round(x, 5) for x in returned] == expected_vec
