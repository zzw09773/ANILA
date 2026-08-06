"""``Embedder.embed`` splits a document into bounded requests.

Why this file exists
--------------------
Before 2026-08-07 a document's whole chunk list went out as one
``input`` array. On the Triton gRPC path CSP embeds one text per
ModelInfer and bounds the **whole call** at ``_wait_ready 5 s +
EMBEDDING_TIMEOUT`` — 35 s at defaults, and explicitly "independent of
``len(texts)``" (``services/csp/app/services/triton_grpc/client.py``
module docstring). A budget that does not grow with the input against an
input that does means every document past some size fails **as a whole**,
and the runbook's remedy ("縮小批次") named an action no operator had a
knob for.

What is pinned here
-------------------
1. **The split happens at all**, and at the configured size. Reverting
   the production change makes ``test_document_larger_than_one_batch_is_split``
   fail on the first assertion (one request instead of three).
2. **The partition is exact** — every text in exactly one batch, in
   order. This is the whole metering argument: CSP bills a request as
   ``sum(per-text)``, so an exact partition makes the batched total
   identical to the unbatched one **on the success path**. Overlapping
   batches would double-bill silently; a dropped text would under-bill
   and lose a chunk. The equality does NOT extend to failed attempts —
   ``test_a_failed_attempt_is_not_free`` pins the other half, because
   "batching doesn't change the bill" is true only of runs that finish.
3. **The partial-failure contract.** A failure aborts: no request is
   sent after the failing batch, nothing partial is returned, and the
   error names the range that died. Each of those is asserted
   separately so that quietly changing the contract (e.g. "skip the bad
   batch and carry on with zeros") turns this file red rather than
   producing a document that is silently missing a slice of itself.

All HTTP is mocked with respx — no network, and no running stack.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from anila_core.ingestion.errors import EmbedError

from ingestion_worker.embedder import Embedder
from ingestion_worker.settings import WorkerSettings


EMBED_URL = "http://embed.test/v1/embeddings"

#: Small enough to eyeball the arithmetic, wide enough that "off by one
#: batch" and "off by one text" look different.
DIM = 4


def _make_settings(**overrides) -> WorkerSettings:
    base = {
        "embedding_base_url": "http://embed.test/v1",
        "embedding_model": "test-model",
        "embedding_api_key": "test-key",
        "embedding_dim": DIM,
        "embedding_timeout_seconds": 5.0,
        "embedding_batch_size": 4,
    }
    base.update(overrides)
    return WorkerSettings(_env_file=None, **base)


def _texts(n: int) -> list[str]:
    """Distinguishable inputs — ``chunk-0`` … so order errors are visible."""
    return [f"chunk-{i}" for i in range(n)]


class _Recorder:
    """respx side-effect that records each request's ``input`` list.

    ``fail_on_batch`` (0-based) makes exactly that request return 503,
    which the Embedder maps to a retryable ``E_EMBED_MODEL_DOWN``.
    """

    def __init__(self, *, fail_on_batch: int | None = None) -> None:
        self.inputs: list[list[str]] = []
        self._fail_on_batch = fail_on_batch

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.inputs.append(body["input"])
        if self._fail_on_batch is not None and len(self.inputs) - 1 == self._fail_on_batch:
            return httpx.Response(503, text="upstream unavailable")
        # One vector per input text, values keyed off the text so a
        # mis-ordered concatenation would be visible.
        vectors = [[float(len(t))] * DIM for t in body["input"]]
        return httpx.Response(200, json={"data": [{"embedding": v} for v in vectors]})

    @property
    def sizes(self) -> list[int]:
        return [len(batch) for batch in self.inputs]

    @property
    def flat(self) -> list[str]:
        return [text for batch in self.inputs for text in batch]


async def _run(embedder: Embedder, texts: list[str], recorder: _Recorder):
    with respx.mock:
        respx.post(EMBED_URL).mock(side_effect=recorder)
        return await embedder.embed(texts)


# ── (a) a document larger than one batch produces several requests ─────────


async def test_document_larger_than_one_batch_is_split():
    """11 chunks at batch_size=4 → three requests of 4 / 4 / 3."""
    embedder = Embedder(_make_settings(embedding_batch_size=4))
    recorder = _Recorder()
    try:
        result = await _run(embedder, _texts(11), recorder)
    finally:
        await embedder.close()

    assert recorder.sizes == [4, 4, 3]
    assert len(result) == 11


async def test_the_split_follows_the_configured_size():
    """The size is the knob's, not a constant baked into the code."""
    embedder = Embedder(_make_settings(embedding_batch_size=3))
    recorder = _Recorder()
    try:
        await _run(embedder, _texts(11), recorder)
    finally:
        await embedder.close()

    assert recorder.sizes == [3, 3, 3, 2]


async def test_no_request_ever_exceeds_the_batch_size():
    """The property the 35 s budget actually depends on."""
    size = 4
    embedder = Embedder(_make_settings(embedding_batch_size=size))
    recorder = _Recorder()
    try:
        await _run(embedder, _texts(30), recorder)
    finally:
        await embedder.close()

    assert recorder.sizes, "no request was sent at all"
    assert max(recorder.sizes) <= size


async def test_order_and_alignment_survive_the_split():
    """Vector *i* still belongs to text *i* after concatenation."""
    embedder = Embedder(_make_settings(embedding_batch_size=4))
    recorder = _Recorder()
    texts = _texts(11)
    try:
        result = await _run(embedder, texts, recorder)
    finally:
        await embedder.close()

    assert recorder.flat == texts
    # _Recorder encodes len(text) into every component of the vector.
    assert [v[0] for v in result] == [float(len(t)) for t in texts]


# ── (b) boundaries ─────────────────────────────────────────────────────────


async def test_exactly_one_batch_is_still_one_request():
    """No empty trailing request when the count divides evenly."""
    embedder = Embedder(_make_settings(embedding_batch_size=4))
    recorder = _Recorder()
    try:
        result = await _run(embedder, _texts(4), recorder)
    finally:
        await embedder.close()

    assert recorder.sizes == [4]
    assert len(result) == 4


async def test_one_chunk_is_one_request():
    embedder = Embedder(_make_settings(embedding_batch_size=4))
    recorder = _Recorder()
    try:
        result = await _run(embedder, _texts(1), recorder)
    finally:
        await embedder.close()

    assert recorder.sizes == [1]
    assert len(result) == 1


async def test_zero_chunks_sends_nothing():
    embedder = Embedder(_make_settings(embedding_batch_size=4))
    recorder = _Recorder()
    try:
        result = await _run(embedder, [], recorder)
    finally:
        await embedder.close()

    assert result == []
    assert recorder.inputs == []


async def test_one_past_a_batch_boundary_adds_a_request_of_one():
    embedder = Embedder(_make_settings(embedding_batch_size=4))
    recorder = _Recorder()
    try:
        await _run(embedder, _texts(5), recorder)
    finally:
        await embedder.close()

    assert recorder.sizes == [4, 1]


async def test_batch_size_larger_than_the_document_is_one_request():
    embedder = Embedder(_make_settings(embedding_batch_size=1000))
    recorder = _Recorder()
    try:
        await _run(embedder, _texts(7), recorder)
    finally:
        await embedder.close()

    assert recorder.sizes == [7]


# ── (c) the partial-failure contract ───────────────────────────────────────
#
# Contract: abort on the first failing batch. Nothing after it is
# requested, nothing partial comes back, and the error says which slice
# of the document died. Every clause is one assertion below.


async def test_a_failing_middle_batch_raises_rather_than_returning_partial():
    embedder = Embedder(_make_settings(embedding_batch_size=4))
    recorder = _Recorder(fail_on_batch=1)
    try:
        with pytest.raises(EmbedError) as excinfo:
            await _run(embedder, _texts(11), recorder)
    finally:
        await embedder.close()

    # Not a shortened list, not zero-vectors standing in for the gap.
    assert excinfo.value.code == "E_EMBED_MODEL_DOWN"


async def test_nothing_is_requested_after_the_failing_batch():
    """Tokens are not spent past a failure that already sank the job."""
    embedder = Embedder(_make_settings(embedding_batch_size=4))
    recorder = _Recorder(fail_on_batch=1)
    try:
        with pytest.raises(EmbedError):
            await _run(embedder, _texts(11), recorder)
    finally:
        await embedder.close()

    assert recorder.sizes == [4, 4], "the third batch was sent after batch 2 failed"


async def test_the_error_names_the_range_that_failed():
    embedder = Embedder(_make_settings(embedding_batch_size=4))
    recorder = _Recorder(fail_on_batch=1)
    try:
        with pytest.raises(EmbedError) as excinfo:
            await _run(embedder, _texts(11), recorder)
    finally:
        await embedder.close()

    details = excinfo.value.details
    assert details["batch_index"] == 1
    assert details["batch_count"] == 3
    assert details["failed_range"] == [4, 8]
    assert details["embedded_before_failure"] == 4
    assert details["input_total"] == 11


async def test_the_user_message_says_which_batch_died():
    """``documents.error_message`` is what the uploader reads."""
    embedder = Embedder(_make_settings(embedding_batch_size=4))
    recorder = _Recorder(fail_on_batch=2)
    try:
        with pytest.raises(EmbedError) as excinfo:
            await _run(embedder, _texts(11), recorder)
    finally:
        await embedder.close()

    message = excinfo.value.user_message
    assert "3/3" in message, message
    # The original endpoint diagnosis is not thrown away for the location.
    assert "HTTP 503" in message, message


async def test_the_failure_keeps_the_original_retryability():
    """The batch a failure lands in says nothing about whether to retry.

    arq's policy reads ``retryable``; rewriting it here would either
    re-run permanent failures three times (three times the tokens) or
    give up on a transient one.
    """
    embedder = Embedder(_make_settings(embedding_batch_size=4))
    recorder = _Recorder(fail_on_batch=1)
    try:
        with pytest.raises(EmbedError) as excinfo:
            await _run(embedder, _texts(11), recorder)
    finally:
        await embedder.close()

    # 503 → retryable in the unbatched path too (see test_embedder.py).
    assert excinfo.value.retryable is True
    assert excinfo.value.severity == "error"
    # The endpoint's own diagnosis survives alongside the batch location.
    assert excinfo.value.details["status_code"] == 503


async def test_a_first_batch_failure_reports_zero_progress():
    embedder = Embedder(_make_settings(embedding_batch_size=4))
    recorder = _Recorder(fail_on_batch=0)
    try:
        with pytest.raises(EmbedError) as excinfo:
            await _run(embedder, _texts(11), recorder)
    finally:
        await embedder.close()

    assert excinfo.value.details["embedded_before_failure"] == 0
    assert recorder.sizes == [4]


# ── (d) metering equality, ON THE SUCCESS PATH ─────────────────────────────
#
# CSP writes one ``token_usage`` row per request with
#   prompt_tokens = sum(max(1, len(t.split())) for t in texts)
# (services/csp/app/services/proxy/service.py). That function is additive
# over texts, so the batched total equals the unbatched total **iff** the
# batches are an exact partition of the input. Both halves are checked:
# the partition here, and the premise itself in
# ``test_the_csp_side_meter_is_still_additive_over_texts``.
#
# ⚠ Read the scope of this section correctly. It says nothing about runs
# that FAIL, and the equality does not extend to them: CSP bills only
# after a successful call (proxy/service.py:291), so a failed document
# used to bill zero and now bills for the batches before the failure —
# again on every arq retry, up to just under 3x a clean pass. That half
# is ``test_a_failed_attempt_is_not_free`` above. "Batching does not
# change the bill" is true only of runs that finish; dropping the
# qualifier here is exactly how the flat claim gets back into the
# operator-facing docs, which is where it was found and removed.

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CSP_PROXY = _REPO_ROOT / "services" / "csp" / "app" / "services" / "proxy" / "service.py"


def _csp_prompt_tokens(texts: list[str]) -> int:
    """The formula CSP bills with, applied to one request's input."""
    return sum(max(1, len(t.split())) for t in texts)


@pytest.mark.parametrize("batch_size", [1, 2, 3, 4, 7, 11, 1000])
async def test_batched_usage_equals_unbatched_usage(batch_size):
    """Same content, any batch size, same bill — when the run succeeds.

    Scope is the success path only. What a run that fails costs is
    ``test_a_failed_attempt_is_not_free``, and it is not the same
    number.
    """
    texts = [
        "一段中文 chunk",
        "another chunk with several words in it",
        "x",
        "",  # empty text — the max(1, …) floor is what makes it cost 1
        "   ",
        "trailing words here",
        "a b c d e f g h i j k",
        "final",
        "one more",
        "and another one",
        "eleven",
    ]
    embedder = Embedder(_make_settings(embedding_batch_size=batch_size))
    recorder = _Recorder()
    try:
        await _run(embedder, texts, recorder)
    finally:
        await embedder.close()

    billed = sum(_csp_prompt_tokens(batch) for batch in recorder.inputs)
    assert billed == _csp_prompt_tokens(texts)


@pytest.mark.parametrize("batch_size", [1, 2, 3, 4, 7, 11, 1000])
async def test_the_batches_are_an_exact_partition(batch_size):
    """No text sent twice (double bill), none dropped (missing chunk).

    Stronger than the equality above and independent of CSP's formula:
    any per-text-additive meter gives the same total over an exact
    partition.
    """
    texts = _texts(11)
    embedder = Embedder(_make_settings(embedding_batch_size=batch_size))
    recorder = _Recorder()
    try:
        await _run(embedder, texts, recorder)
    finally:
        await embedder.close()

    assert recorder.flat == texts


async def test_a_failed_attempt_is_not_free():
    """The half of the metering story the success-path tests cannot see.

    CSP writes a ``token_usage`` row only after a request succeeds
    (``proxy/service.py:291``). Unbatched, a failed document was one
    request, one failure, **zero** rows. Batched, the batches before the
    failure each succeeded and each billed — and arq re-runs the whole
    job up to ``max_tries=3``, so those are paid for again every retry.

    This exists so nobody can restore the flat claim "batching does not
    change the bill" without a red test. It pins the observable fact the
    docs' measured numbers rest on: a failed call has already spent
    ``embedded_before_failure`` texts' worth of billable requests.
    """
    embedder = Embedder(_make_settings(embedding_batch_size=4))
    recorder = _Recorder(fail_on_batch=2)
    try:
        with pytest.raises(EmbedError) as excinfo:
            await _run(embedder, _texts(20), recorder)
    finally:
        await embedder.close()

    billed_requests = recorder.inputs[:-1]  # the last one is the 503
    assert billed_requests, "a failed attempt billed nothing — the docs' 帳單 table is wrong"
    billed_texts = sum(len(b) for b in billed_requests)
    assert billed_texts == excinfo.value.details["embedded_before_failure"] == 8
    # ...and the document is still lost, so this is spend with no result.
    assert excinfo.value.details["input_total"] == 20


def test_the_csp_side_meter_is_still_additive_over_texts():
    """The premise the equality argument rests on, read from CSP's source.

    If CSP ever bills embeddings by something that is not a sum over the
    individual texts — a per-request floor, a shared prefix discount —
    then splitting a document into more requests changes the bill, and
    the batch size stops being a purely operational knob. This test
    going red means "re-derive the metering argument", not "fix CSP".
    """
    # Deliberately NOT a skip. This is the only test holding the
    # metering-equality argument up, and a skip is invisible in a green
    # run — moving this file one directory deeper would have retired the
    # guard without anyone deciding to. Its sibling
    # test_compose_worker_env_passthrough.py has no escape hatch either.
    assert _CSP_PROXY.is_file(), (
        f"找不到 {_CSP_PROXY} —— 這條是「分批不改變成功路徑帳單」這個論證的唯一"
        f"支點,路徑推導壞掉時它必須紅,不能安靜跳過。tests/ 若搬過位置,修正 "
        f"_REPO_ROOT 的 parents[3];csp 若真的不在這個 checkout 裡,那要先決定"
        f"由誰來釘這個前提,不是讓它自己消失"
    )
    source = _CSP_PROXY.read_text(encoding="utf-8")
    assert "prompt_tokens = sum(max(1, len(t.split())) for t in texts)" in source, (
        "CSP 的 embedding 計量式不再是「逐段文字相加」—— 分批就會改變帳單金額,"
        f"批次大小不再只是運維旋鈕。請重新推導後更新本檔({_CSP_PROXY})"
    )
