"""Live final-output SSE, cancellation, and caller authorization regressions."""

import asyncio
import json

import pytest

from app.models.quick_builder import QuickRun
from app.services import internal_llm, quick_model_stream
from app.services.quick_execution import agent_response
from app.services.quick_flow import execute_graph
from tests.test_quick_builder_review import (
    _assistant,
    _graph,
    review_rows as _review_rows,
)

review_rows = _review_rows


def _delta(frame):
    if not frame.startswith("data: {"):
        return ""
    data = json.loads(frame[6:])
    return data.get("choices", [{}])[0].get("delta", {}).get("content", "")


@pytest.mark.asyncio
async def test_final_model_delta_arrives_before_model_completion(
    db, review_rows, monkeypatch
):
    _, user, _, _, _ = review_rows
    _, _, agent = _assistant(db, review_rows)
    finished = asyncio.Event()

    async def synthetic(db, model, body, *, on_delta, before_chunk, **kwargs):
        before_chunk()
        await on_delta("第一段合成回答。")
        finished.set()
        await on_delta("後續合成回答。")
        return "第一段合成回答。後續合成回答。"

    monkeypatch.setattr(quick_model_stream, "stream_chat", synthetic)
    response = await agent_response(
        db,
        user,
        agent,
        {
            "stream": True,
            "messages": [{"role": "user", "content": "合成問題"}],
        },
    )
    iterator = response.body_iterator
    first = await asyncio.wait_for(anext(iterator), 2)
    assert _delta(first) == "第一段合成回答。"
    assert not finished.is_set()
    rest = [frame async for frame in iterator]
    assert "".join(map(_delta, [first, *rest])) == "第一段合成回答。後續合成回答。"
    assert rest[-1] == "data: [DONE]\n\n"
    db.expire_all()
    assert db.query(QuickRun).one().status == "completed"


@pytest.mark.asyncio
async def test_heartbeat_backpressure_does_not_lose_the_first_delta(
    db, review_rows, monkeypatch
):
    from app.services import quick_stream_response

    _, user, _, _, _ = review_rows
    _, _, agent = _assistant(db, review_rows)
    release = asyncio.Event()
    original_wait = asyncio.wait

    async def short_heartbeat(*args, **kwargs):
        if kwargs.get("timeout") == 15:
            kwargs["timeout"] = 0.01
        return await original_wait(*args, **kwargs)

    async def synthetic(*args, on_delta, **kwargs):
        await release.wait()
        await on_delta("合成首段回答。")
        return "合成首段回答。"

    monkeypatch.setattr(quick_stream_response.asyncio, "wait", short_heartbeat)
    monkeypatch.setattr(quick_model_stream, "stream_chat", synthetic)
    response = await agent_response(
        db,
        user,
        agent,
        {
            "stream": True,
            "messages": [{"role": "user", "content": "合成問題"}],
        },
    )
    iterator = response.body_iterator
    first = await asyncio.wait_for(anext(iterator), 2)
    assert first.startswith(": keep-alive")
    release.set()
    await asyncio.sleep(0.03)
    next_frame = await asyncio.wait_for(anext(iterator), 2)
    assert _delta(next_frame) == "合成首段回答。"
    remaining = [frame async for frame in iterator]
    assert remaining[-1] == "data: [DONE]\n\n"


@pytest.mark.asyncio
async def test_cancel_after_first_delta_cancels_model_and_run(
    db, review_rows, monkeypatch
):
    _, user, _, _, _ = review_rows
    _, _, agent = _assistant(db, review_rows)
    closed = asyncio.Event()

    async def synthetic(db, model, body, *, on_delta, **kwargs):
        try:
            await on_delta("合成可見回答。")
            await asyncio.Event().wait()
        finally:
            closed.set()

    monkeypatch.setattr(quick_model_stream, "stream_chat", synthetic)
    response = await agent_response(
        db,
        user,
        agent,
        {
            "stream": True,
            "messages": [{"role": "user", "content": "合成問題"}],
        },
    )
    iterator = response.body_iterator
    assert _delta(await asyncio.wait_for(anext(iterator), 2))
    await asyncio.wait_for(iterator.aclose(), 2)
    assert closed.is_set()
    db.expire_all()
    assert db.query(QuickRun).one().status == "cancelled"


@pytest.mark.asyncio
async def test_revocation_during_backpressure_blocks_next_delta(
    db, review_rows, monkeypatch
):
    _, user, _, _, _ = review_rows
    assistant, _, agent = _assistant(db, review_rows)

    async def synthetic(db, model, body, *, on_delta, **kwargs):
        await on_delta("前一段合成回答。")
        await on_delta("REVOKED_OUTPUT_SENTINEL")
        return "前一段合成回答。REVOKED_OUTPUT_SENTINEL"

    monkeypatch.setattr(quick_model_stream, "stream_chat", synthetic)
    response = await agent_response(
        db,
        user,
        agent,
        {
            "stream": True,
            "messages": [{"role": "user", "content": "合成問題"}],
        },
    )
    iterator = response.body_iterator
    first = await asyncio.wait_for(anext(iterator), 2)
    assistant.disabled = True
    db.commit()
    rest = [frame async for frame in iterator]
    assert "REVOKED_OUTPUT_SENTINEL" not in "".join([first, *rest])
    assert "anila.error" in "".join(rest)
    db.expire_all()
    assert db.query(QuickRun).one().status == "failed"


@pytest.mark.asyncio
async def test_stream_masks_secrets_split_across_model_deltas(
    db, review_rows, monkeypatch
):
    _, user, _, _, _ = review_rows
    _, _, agent = _assistant(db, review_rows)
    raw = "合成結果。 password=SYNTHETIC_PRIVATE_VALUE"

    async def synthetic(db, model, body, *, on_delta, **kwargs):
        for char in raw:
            await on_delta(char)
        return raw

    monkeypatch.setattr(quick_model_stream, "stream_chat", synthetic)
    response = await agent_response(
        db,
        user,
        agent,
        {
            "stream": True,
            "messages": [{"role": "user", "content": "合成問題"}],
        },
    )
    frames = [frame async for frame in response.body_iterator]
    output = "".join(map(_delta, frames))
    assert "合成結果。" in output and "[已遮罩]" in output
    assert "SYNTHETIC_PRIVATE_VALUE" not in "".join(frames)
    assert "anila.error" not in "".join(frames)


@pytest.mark.asyncio
async def test_nonterminal_and_agent_planning_use_buffered_callback_only():
    calls = []

    async def ordinary(messages, temperature):
        calls.append("ordinary")
        if "規劃下一步" in json.dumps(messages, ensure_ascii=False):
            return '{"action":"final","output":"合成代理結果"}'
        return "中間合成結果"

    async def live(messages, temperature):
        calls.append("live")
        return "最後合成結果"

    async def unused(*args, **kwargs):
        raise AssertionError("no external callback expected")

    graph = _graph(
        {"id": "model", "kind": "model"},
        {"id": "agent", "kind": "agent", "config": {"tool_ids": [1]}},
        {"id": "format", "kind": "format"},
    )
    result = await execute_graph(
        graph,
        "問題",
        model_call=ordinary,
        stream_model_call=live,
        rag_call=unused,
        tool_call=unused,
        document_call=unused,
    )
    assert result["status"] == "completed"
    assert calls == ["ordinary", "ordinary", "live"]


@pytest.mark.asyncio
async def test_models_without_streaming_keep_a_valid_sse_response(
    db, review_rows, monkeypatch
):
    _, user, _, model, _ = review_rows
    _, _, agent = _assistant(db, review_rows)
    model.supports_streaming = False
    db.commit()

    async def ordinary(*args, **kwargs):
        return "合成完整回答"

    async def forbidden(*args, **kwargs):
        raise AssertionError("non-streaming model must use complete_chat")

    monkeypatch.setattr(internal_llm, "complete_chat", ordinary)
    monkeypatch.setattr(quick_model_stream, "stream_chat", forbidden)
    response = await agent_response(
        db,
        user,
        agent,
        {
            "stream": True,
            "messages": [{"role": "user", "content": "合成問題"}],
        },
    )
    frames = [frame async for frame in response.body_iterator]
    assert "".join(map(_delta, frames)) == "合成完整回答"
    assert frames[-1] == "data: [DONE]\n\n"


@pytest.mark.asyncio
async def test_identity_change_before_generator_starts_sends_no_content(
    db, review_rows, monkeypatch
):
    _, user, _, _, _ = review_rows
    _, _, agent = _assistant(db, review_rows)

    async def forbidden(*args, **kwargs):
        raise AssertionError("stale authenticated identity cannot call a model")

    monkeypatch.setattr(quick_model_stream, "stream_chat", forbidden)
    response = await agent_response(
        db,
        user,
        agent,
        {
            "stream": True,
            "messages": [{"role": "user", "content": "合成問題"}],
        },
    )
    user.token_version += 1
    db.commit()
    frames = [frame async for frame in response.body_iterator]
    assert not any(_delta(frame) for frame in frames)
    assert "anila.error" in "".join(frames)
    assert db.query(QuickRun).count() == 0


@pytest.mark.asyncio
async def test_stream_owns_session_after_request_session_closes(
    db, review_rows, monkeypatch
):
    _, user, _, _, _ = review_rows
    _, _, agent = _assistant(db, review_rows)

    async def synthetic(db, model, body, *, on_delta, **kwargs):
        await on_delta("合成獨立連線回答。")
        return "合成獨立連線回答。"

    monkeypatch.setattr(quick_model_stream, "stream_chat", synthetic)
    response = await agent_response(
        db,
        user,
        agent,
        {
            "stream": True,
            "messages": [{"role": "user", "content": "合成問題"}],
        },
    )
    db.close()
    frames = [frame async for frame in response.body_iterator]
    assert "".join(map(_delta, frames)) == "合成獨立連線回答。"
    assert db.query(QuickRun).one().status == "completed"


@pytest.mark.asyncio
async def test_visible_output_limit_has_typed_error_and_failed_run(
    db, review_rows, monkeypatch
):
    _, user, _, _, _ = review_rows
    _, _, agent = _assistant(db, review_rows)

    async def synthetic(db, model, body, *, on_delta, **kwargs):
        await on_delta("文" * 20000)
        await on_delta("終")
        return "文" * 20000 + "終"

    monkeypatch.setattr(quick_model_stream, "stream_chat", synthetic)
    response = await agent_response(
        db,
        user,
        agent,
        {
            "stream": True,
            "messages": [{"role": "user", "content": "合成問題"}],
        },
    )
    frames = [frame async for frame in response.body_iterator]
    assert "OUTPUT_TOO_LARGE" in "".join(frames)
    assert "終" not in "".join(map(_delta, frames))
    db.expire_all()
    assert db.query(QuickRun).one().status == "failed"


@pytest.mark.asyncio
async def test_cancel_final_document_delivery_invalidates_saved_result(db, review_rows):
    _, user, _, _, _ = review_rows
    _, _, agent = _assistant(
        db,
        review_rows,
        graph=_graph(
            {
                "id": "document",
                "kind": "document",
                "config": {"template": "{{text}}"},
            }
        ),
    )
    response = await agent_response(
        db,
        user,
        agent,
        {
            "stream": True,
            "messages": [{"role": "user", "content": "合成文件"}],
        },
    )
    iterator = response.body_iterator
    assert _delta(await asyncio.wait_for(anext(iterator), 2))
    await asyncio.wait_for(iterator.aclose(), 2)
    db.expire_all()
    stored = db.query(QuickRun).one()
    assert stored.status == "cancelled" and stored.result["artifacts"] == []


@pytest.mark.asyncio
async def test_document_classification_upgrade_during_delivery_stops_remaining_output(
    db, review_rows, monkeypatch
):
    from tests.test_quick_execution_context import _task_snapshot
    from app.services.proxy import task_link

    _, user, _, _, department = review_rows
    _, _, agent = _assistant(
        db,
        review_rows,
        graph=_graph(
            {
                "id": "document",
                "kind": "document",
                "config": {"template": "{{text}}"},
            }
        ),
    )
    task, _, task_ctx = _task_snapshot(db, user, department)
    monkeypatch.setattr(task_link, "finalize_task_run", lambda *args: None)
    response = await agent_response(
        db,
        user,
        agent,
        {
            "stream": True,
            "messages": [{"role": "user", "content": "合成長文件。" * 100}],
        },
        task_ctx=task_ctx,
    )
    iterator = response.body_iterator
    assert _delta(await asyncio.wait_for(anext(iterator), 2))
    task.classification_level = "機密"
    db.commit()
    remaining = "".join([frame async for frame in iterator])
    assert "anila.error" in remaining
    assert "/artifacts/" not in remaining
    db.expire_all()
    stored = db.query(QuickRun).one()
    assert stored.status == "failed" and stored.result["artifacts"] == []


@pytest.mark.asyncio
async def test_generator_exit_has_terminal_error_without_waiting(
    db, review_rows, monkeypatch
):
    _, user, _, _, _ = review_rows
    _, _, agent = _assistant(db, review_rows)

    async def stopped(*args, **kwargs):
        raise GeneratorExit

    monkeypatch.setattr(quick_model_stream, "stream_chat", stopped)
    response = await agent_response(
        db,
        user,
        agent,
        {
            "stream": True,
            "messages": [{"role": "user", "content": "合成問題"}],
        },
    )

    async def consume():
        return "".join([frame async for frame in response.body_iterator])

    wire = await asyncio.wait_for(consume(), 2)
    assert "anila.error" in wire and "[DONE]" in wire
    db.expire_all()
    assert db.query(QuickRun).one().status == "cancelled"


@pytest.mark.asyncio
async def test_failed_invalidation_still_terminates_stream(
    db, review_rows, monkeypatch
):
    from app.services import quick_execution

    _, user, _, _, _ = review_rows
    assistant, _, agent = _assistant(
        db,
        review_rows,
        graph=_graph(
            {
                "id": "document",
                "kind": "document",
                "config": {"template": "{{text}}"},
            }
        ),
    )
    original_finish = quick_execution._finish

    def finish(*args, **kwargs):
        if args[4]["status"] != "completed":
            raise RuntimeError("synthetic persistence unavailable")
        return original_finish(*args, **kwargs)

    monkeypatch.setattr(quick_execution, "_finish", finish)
    response = await agent_response(
        db,
        user,
        agent,
        {
            "stream": True,
            "messages": [{"role": "user", "content": "合成長文件。" * 100}],
        },
    )
    iterator = response.body_iterator
    assert _delta(await asyncio.wait_for(anext(iterator), 2))
    assistant.disabled = True
    db.commit()

    async def consume():
        return "".join([frame async for frame in iterator])

    wire = await asyncio.wait_for(consume(), 2)
    assert "STREAM_PERSIST_FAILED" in wire and "[DONE]" in wire


@pytest.mark.parametrize(
    "value",
    [
        '{"password":"SYNTHETIC_PRIVATE_VALUE"}',
        '{"\\u0070assword":"SYNTHETIC_PRIVATE_VALUE"}',
        '內容：{"password":"SYNTHETIC_PRIVATE_VALUE"}',
        '內容：{"\\u0070assword":"SYNTHETIC_PRIVATE_VALUE"}',
        '{"password":"SYNTHETIC_PRIVATE_VALUE',
        '內容：int value; "\\u0070assword": "SYNTHETIC_PRIVATE_VALUE',
        'C++ f(){value;} {"password' + ("x" * 600) + '":"SYNTHETIC_PRIVATE_VALUE"}',
    ],
)
def test_json_prefix_with_source_suffix_masks_sensitive_values(value):
    from app.services.quick_execution import mask

    output = mask(value + "\n\n來源：\n- 合成來源")
    assert "SYNTHETIC_PRIVATE_VALUE" not in output
    assert "已遮罩" in output
    assert "int main() { return 0; }" == mask("int main() { return 0; }")


@pytest.mark.asyncio
@pytest.mark.parametrize("live", [False, True])
async def test_json_answer_with_rag_sources_never_leaks_or_false_fails(
    db, review_rows, monkeypatch, live
):
    from app.models.ingestion import IngestionCollection
    from app.models.quick_builder import QuickCollectionPublication
    from app.services import quick_retrieval

    owner, user, admin, model, department = review_rows
    collection = IngestionCollection(
        name="合成公開資料",
        created_by=owner.id,
        department_id=department.id,
        embedding_model="synthetic",
        embedding_dim=3,
        chunking_config={},
        status="active",
    )
    db.add(collection)
    db.flush()
    db.add(
        QuickCollectionPublication(
            collection_id=collection.id, scope="unit", approved_by=admin.id
        )
    )
    model.supports_streaming = live
    db.commit()
    _, _, agent = _assistant(
        db,
        review_rows,
        graph=_graph(
            {"id": "rag", "kind": "rag", "config": {"collection_ids": [collection.id]}},
            {"id": "model", "kind": "model"},
        ),
        collection_ids=[collection.id],
    )
    raw = '{"password":"SYNTHETIC_PRIVATE_VALUE","answer":"合成答案"}'

    async def retrieve(*args, **kwargs):
        return [{"filename": "合成來源", "content": "合成依據", "chunk_id": "chunk-1"}]

    async def ordinary(*args, **kwargs):
        return raw

    async def live_model(*args, on_delta, **kwargs):
        await on_delta(raw)
        return raw

    monkeypatch.setattr(quick_retrieval, "retrieve_collections", retrieve)
    monkeypatch.setattr(internal_llm, "complete_chat", ordinary)
    monkeypatch.setattr(quick_model_stream, "stream_chat", live_model)
    response = await agent_response(
        db,
        user,
        agent,
        {
            "stream": True,
            "messages": [{"role": "user", "content": "合成問題"}],
        },
    )
    frames = [frame async for frame in response.body_iterator]
    wire = "".join(frames)
    assert "SYNTHETIC_PRIVATE_VALUE" not in wire
    assert "anila.error" not in wire
    assert "合成來源" in wire
    db.expire_all()
    assert db.query(QuickRun).one().status == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("depth", [11, 12, 13, 1200])
async def test_deep_json_is_masked_and_run_reaches_terminal_state(
    db, review_rows, monkeypatch, depth
):
    _, user, _, _, _ = review_rows
    _, _, agent = _assistant(db, review_rows)
    raw = "[" * depth + '"safe"' + "]" * depth

    async def live_model(*args, on_delta, **kwargs):
        await on_delta(raw)
        return raw

    monkeypatch.setattr(quick_model_stream, "stream_chat", live_model)
    response = await agent_response(
        db,
        user,
        agent,
        {
            "stream": True,
            "messages": [{"role": "user", "content": "合成問題"}],
        },
    )
    frames = [frame async for frame in response.body_iterator]
    assert "[DONE]" in "".join(frames)
    assert "anila.error" not in "".join(frames)
    db.expire_all()
    assert db.query(QuickRun).one().status == "completed"
