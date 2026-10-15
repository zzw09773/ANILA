"""Pool connections must be released before SSE handoff and backpressure."""

import asyncio
import json

import pytest

from app.services.quick_execution import agent_response, run
from app.services.proxy import service as proxy_service
from tests.test_quick_builder_review import _assistant, review_rows as _review_rows


review_rows = _review_rows


@pytest.mark.asyncio
async def test_sse_handoff_releases_original_request_read_transaction(db, review_rows):
    _, user, _, _, _ = review_rows
    _, _, agent = _assistant(db, review_rows)
    db.commit()
    assert not db.in_transaction()
    response = await agent_response(
        db,
        user,
        agent,
        {
            "stream": True,
            "messages": [{"role": "user", "content": "合成連線交接反例"}],
        },
    )
    try:
        # QuickVersion was really selected; keeping this request's connection
        # until stream teardown can starve the new stream-owned Session.
        assert not db.in_transaction()
    finally:
        await response.body_iterator.aclose()


@pytest.mark.asyncio
async def test_real_run_delivery_guards_release_connection_before_waiting_for_ack(
    db,
    review_rows,
    monkeypatch,
):
    _, user, _, _, _ = review_rows
    assistant, version, _ = _assistant(db, review_rows)
    text, observed = "合成串流連線回答。", []

    async def synthetic_transport(**kwargs):
        yield (
            "data: "
            + json.dumps(
                {
                    "choices": [
                        {"index": 0, "delta": {"content": text}, "finish_reason": None}
                    ]
                }
            )
            + "\n\n"
        )
        yield 'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
        yield "data: [DONE]\n\n"

    async def awaiting_ack(delta, *, before_send):
        # The real quick_model_stream.emit called before_chunk immediately
        # before this callback; its reauthorization must not retain a readtx.
        assert not db.in_transaction()
        before_send()
        assert not db.in_transaction()
        observed.append(delta)
        await asyncio.sleep(0)
        assert not db.in_transaction()

    monkeypatch.setattr(proxy_service, "proxy_stream", synthetic_transport)
    result = await run(
        db, user, assistant.id, version.id, "合成池釋放問題", on_delta=awaiting_ack
    )
    assert result["status"] == "completed"
    assert result["output"] == text
    assert "".join(observed) == text
