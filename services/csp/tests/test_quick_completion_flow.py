"""Completion semantics through flow state, masked trace and Agent responses."""
from __future__ import annotations

import asyncio
import json

import pytest

from app.models.quick_builder import QuickRun
from app.services import quick_flow
from app.services.completion_metadata import completion_metadata
from app.services.quick_execution import agent_response, public_result
from app.services.quick_flow import execute_graph
from app.services.proxy.sse import _parse_sse_block
from tests.test_quick_flow import chain, node
from tests.test_quick_model_stream import frame, gateway as _gateway
from tests.test_quick_builder_review import _assistant, review_rows as _review_rows
from tests.test_quick_stream_terminal import finish
from tests.test_quick_builder import create, login, http as _http, people as _people

gateway = _gateway
review_rows = _review_rows
http = _http
people = _people


def result(text="合成部分回答", reason="length", usage=None):
    return {
        "text": text,
        "completion": completion_metadata(
            finish_reason=reason, terminal_source="finish_reason", usage=usage,
        ),
    }


async def unused(*args, **kwargs):
    raise AssertionError("unexpected provider")


def run(graph, model, *, stream=False, tool=unused, document=unused):
    return asyncio.run(execute_graph(
        graph, "合成問題", model_call=model, rag_call=unused,
        tool_call=tool, document_call=document,
        stream_model_call=model if stream else None,
    ))


@pytest.mark.parametrize("reason", ["stop", "length", "content_filter", "tool_calls", "function_call"])
@pytest.mark.parametrize("kind", ["model", "format"])
@pytest.mark.parametrize("stream", [False, True])
def test_terminal_completion_retains_reason_and_trace(reason, kind, stream):
    async def model(*args):
        return result(reason=reason, usage={"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12})

    output = run(chain(node("m", kind)), model, stream=stream)
    complete = reason == "stop"
    assert output["status"] == "completed"
    assert output["answer_complete"] is complete
    assert output["completion"]["finish_reason"] == reason
    assert output["completion"]["usage_complete"] is True
    assert output["completion"]["can_continue"] is False
    assert output["completion"]["usage"]["total_tokens"] == 12
    assert output["steps"][1]["status"] == ("completed" if complete else "incomplete")
    assert output["steps"][1]["model_completions"] == [output["steps"][1]["completion"]]
    assert output["steps"][-1]["status"] == ("completed" if complete else "incomplete")


@pytest.mark.parametrize("kind", ["model", "format"])
def test_empty_content_filter_remains_an_explicit_incomplete_answer(kind):
    async def model(*args):
        return result("", "content_filter")

    output = run(chain(node("m", kind)), model)
    assert output["status"] == "completed" and output["output"] == ""
    assert output["answer_complete"] is False
    assert output["completion"]["type"] == "MODEL_CONTENT_FILTER"


@pytest.mark.parametrize("reason", ["length", "content_filter"])
@pytest.mark.parametrize("kind", ["model", "extract", "agent"])
def test_intermediate_completion_cannot_trigger_tools_documents_or_plans(reason, kind):
    calls = []

    async def model(*args):
        calls.append("model")
        return result('{"action":"tool","tool_id":7,"arguments":{}}', reason)

    async def tool(*args):
        calls.append("tool")
        return {"ok": True}

    async def document(*args):
        calls.append("document")
        return {"text": "不應產生"}

    config = {"fields": ["action", "tool_id", "arguments"]} if kind == "extract" else {"tool_ids": [7]} if kind == "agent" else {}
    output = run(chain(node("m", kind, **config), node("t", "mcp", tool_id=7), node("d", "document")), model, tool=tool, document=document)
    assert calls == ["model"]
    assert output["status"] == "failed" and output["answer_complete"] is False
    assert output["completion"]["finish_reason"] == reason
    assert output["error"]["message"] == output["completion"]["message"]
    assert output["error"]["message"].count(output["completion"]["type"]) == 1


def test_completion_aggregate_counts_each_model_once_and_suppresses_provider_fields():
    calls = 0

    async def model(*args):
        nonlocal calls
        calls += 1
        value = result(reason="stop" if calls == 1 else "length", usage={"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5, "api_key": "SYNTHETIC_PROVIDER_CANARY"})
        value["completion"]["message"] = "SYNTHETIC_PROVIDER_CANARY https://provider.invalid/private"
        return value

    output = run(chain(node("a", "model"), node("b", "model")), model)
    assert output["completion"]["usage"] == {"prompt_tokens": 4, "completion_tokens": 6, "total_tokens": 10}
    assert output["completion"]["finish_reason"] == "length"
    assert "SYNTHETIC_PROVIDER_CANARY" not in json.dumps(output)


def test_aggregate_usage_overflow_keeps_reason_and_per_model_values():
    calls = 0

    async def model(*args):
        nonlocal calls
        calls += 1
        return result(reason="stop" if calls == 1 else "length", usage={"total_tokens": 2**53 - 1})

    output = run(chain(node("a", "model"), node("b", "model")), model)
    assert output["status"] == "completed" and output["answer_complete"] is False
    assert output["completion"]["finish_reason"] == "length"
    assert output["completion"]["usage"] == {}
    assert output["completion"]["usage_complete"] is False
    assert output["steps"][1]["completion"]["usage"]["total_tokens"] == 2**53 - 1


@pytest.mark.parametrize("failure", ["model", "mcp", "timeout"])
def test_aggregate_distinguishes_unknown_model_usage_from_non_model_failure(failure, monkeypatch):
    calls = 0

    async def model(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("SYNTHETIC_PROVIDER_CANARY")
        return result(reason="stop", usage={"total_tokens": 12})

    async def tool(*args):
        if failure == "timeout":
            await asyncio.Event().wait()
        raise RuntimeError("SYNTHETIC_PROVIDER_CANARY")

    if failure == "timeout":
        monkeypatch.setattr(quick_flow, "EXECUTION_TIMEOUT_SECONDS", 0.02)
    output = run(chain(node("a", "model"), node("b", failure if failure == "model" else "mcp", **({} if failure == "model" else {"tool_id": 7}))), model, tool=tool)
    assert output["status"] == "failed" and output["answer_complete"] is False
    assert output["completion"]["usage"] == {"total_tokens": 12}
    assert output["completion"]["usage_complete"] is (failure != "model")
    assert "SYNTHETIC_PROVIDER_CANARY" not in json.dumps(output)


def test_invalid_callback_completion_is_not_a_normal_text_response():
    async def model(*args):
        value = result()
        value["completion"]["answer_complete"] = True
        return value

    output = run(chain(node("m", "model")), model)
    assert output["status"] == "failed" and output["error"]["type"] == "MODEL_RESPONSE"


def test_public_trace_projects_only_validated_completion_usage():
    meta = completion_metadata(finish_reason="length", terminal_source="finish_reason", usage={"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12})
    value = {"status": "completed", "answer_complete": False, "completion": meta,
             "steps": [{"completion": meta, "model_completions": [meta], "tool": [{"result": {"prompt_tokens": "SYNTHETIC_PROVIDER_CANARY", "api_key": "SYNTHETIC_PROVIDER_CANARY"}}]}]}
    output = public_result(1, value, [])
    assert output["completion"]["usage"]["total_tokens"] == 12
    assert output["steps"][0]["completion"]["usage"]["total_tokens"] == 12
    assert output["steps"][0]["model_completions"][0]["usage"]["total_tokens"] == 12
    assert "SYNTHETIC_PROVIDER_CANARY" not in json.dumps(output)


@pytest.mark.parametrize("reason", ["stop", "length", "content_filter"])
@pytest.mark.parametrize("reported", [False, True])
def test_real_proxy_stream_reason_usage_and_stored_trace(db, gateway, review_rows, reason, reported):
    _, user, _, model, _ = review_rows
    _, _, agent = _assistant(db, review_rows)
    gateway.user, gateway.model = user, model
    usage = {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12} if reported else None
    gateway.chunks = [frame("合成部分回答", usage=usage), finish(reason)]

    async def scenario():
        response = await agent_response(db, user, agent, {"stream": True, "messages": [{"role": "user", "content": "合成提問"}]})
        return [chunk async for chunk in response.body_iterator]

    frames = asyncio.run(scenario())
    assert frames[-1] == "data: [DONE]\n\n"
    parsed = [_parse_sse_block(value.strip()) for value in frames]
    metadata = [json.loads(data) for event, data in parsed if event == "anila.meta"][-1]
    finishes = [json.loads(data)["choices"][0]["finish_reason"] for event, data in parsed if event is None and data.startswith("{") and json.loads(data).get("choices", [{}])[0].get("finish_reason")]
    assert finishes == [reason]
    assert metadata["completion"]["answer_complete"] is (reason == "stop")
    assert metadata["usage_complete"] is True
    assert metadata["completion"]["usage"] == metadata["usage"]
    assert len(gateway.normal) == 1 and gateway.partial == []
    assert gateway.normal[0]["user_id"] == user.id
    assert gateway.normal[0]["token_source"] == ("reported" if reported else "estimated")
    db.expire_all()
    stored = db.query(QuickRun).one()
    assert stored.status == "completed"
    assert stored.result["completion"] == metadata["completion"]
    assert stored.result["steps"][1]["completion"]["usage"] == metadata["usage"]
    if reported:
        assert stored.result["completion"]["usage"]["total_tokens"] == 12


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("reason,text", [("stop", "正常合成回答"), ("length", "部分合成回答"), ("content_filter", "")])
def test_json_and_stream_same_completion_contract(db, review_rows, monkeypatch, reason, text, stream):
    from app.services import internal_llm, quick_model_stream
    _, user, _, _, _ = review_rows
    _, _, agent = _assistant(db, review_rows)

    async def model(*args, **kwargs):
        assert kwargs["return_metadata"] is True
        if "on_delta" in kwargs and text:
            kwargs["before_chunk"]()
            await kwargs["on_delta"](text)
        return result(text, reason, {"total_tokens": 12})

    monkeypatch.setattr(internal_llm, "complete_chat", model)
    monkeypatch.setattr(quick_model_stream, "stream_chat", model)

    async def scenario():
        response = await agent_response(db, user, agent, {"stream": stream, "messages": [{"role": "user", "content": "合成提問"}]})
        if stream:
            frames = [chunk async for chunk in response.body_iterator]
            metadata = [json.loads(data) for event, data in map(lambda item: _parse_sse_block(item.strip()), frames) if event == "anila.meta"][-1]
            return metadata
        value = json.loads(response.body)
        assert value["choices"][0]["finish_reason"] == reason
        assert value["choices"][0]["message"]["content"] == text
        return value["anila_meta"]

    metadata = asyncio.run(scenario())
    assert metadata["completion"]["finish_reason"] == reason
    assert metadata["completion"]["answer_complete"] is (reason == "stop")
    assert metadata["completion"]["can_continue"] is False
    assert metadata["usage"] == {"total_tokens": 12}


def test_real_proxy_empty_filter_keeps_legitimate_usage_and_reason(db, gateway, review_rows):
    _, user, _, model, _ = review_rows
    _, _, agent = _assistant(db, review_rows)
    gateway.user, gateway.model = user, model
    gateway.chunks = [frame("", usage={"prompt_tokens": 8, "completion_tokens": 0, "total_tokens": 8}), finish("content_filter")]

    async def scenario():
        response = await agent_response(db, user, agent, {"stream": True, "messages": [{"role": "user", "content": "合成提問"}]})
        return "".join([chunk async for chunk in response.body_iterator])

    output = asyncio.run(scenario())
    assert "MODEL_CONTENT_FILTER" in output and "anila.error" not in output
    assert len(gateway.normal) == 1 and gateway.partial == []
    db.expire_all()
    stored = db.query(QuickRun).one()
    assert stored.result["answer_complete"] is False
    assert stored.result["completion"]["usage"]["total_tokens"] == 8


@pytest.mark.parametrize("reason", ["length", "content_filter"])
@pytest.mark.parametrize("stream", [False, True])
def test_incomplete_intermediate_reason_remains_on_failed_public_exit(db, review_rows, monkeypatch, reason, stream):
    from fastapi import HTTPException
    from app.services import internal_llm
    _, user, _, _, _ = review_rows
    _, _, agent = _assistant(db, review_rows, graph=chain(node("a", "model"), node("b", "model")))
    calls = 0

    async def model(*args, **kwargs):
        nonlocal calls
        calls += 1
        return result(reason=reason, usage={"total_tokens": 12})

    monkeypatch.setattr(internal_llm, "complete_chat", model)

    async def scenario():
        try:
            response = await agent_response(db, user, agent, {"stream": stream, "messages": [{"role": "user", "content": "合成提問"}]})
        except HTTPException as exc:
            assert stream is False and exc.status_code == 502
            return exc.detail
        frames = [chunk async for chunk in response.body_iterator]
        parsed = [_parse_sse_block(value.strip()) for value in frames]
        assert any(event == "anila.error" for event, _ in parsed)
        assert not any(event is None and '"finish_reason": "stop"' in data for event, data in parsed)
        return [json.loads(data) for event, data in parsed if event == "anila.meta"][-1]

    value = asyncio.run(scenario())
    assert calls == 1
    assert value["completion"]["finish_reason"] == reason
    assert value["completion"]["answer_complete"] is False
    assert value["completion"]["usage"] == {"total_tokens": 12}
    db.expire_all()
    assert db.query(QuickRun).one().status == "failed"


@pytest.mark.parametrize("reason", ["length", "content_filter"])
def test_authenticated_http_test_and_reload_keep_safe_termination(http, people, monkeypatch, reason):
    from app.services import internal_llm

    async def model(*args, **kwargs):
        assert kwargs["return_metadata"] is True
        return result("部分合成回答" if reason == "length" else "", reason, {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12})

    monkeypatch.setattr(internal_llm, "complete_chat", model)
    item, _, headers = create(http, people, graph=chain(node("m", "model")))
    response = http.post(
        f"/api/quick-builder/{item['id']}/test",
        json={"version_id": item["versions"][0]["id"], "message": "合成問題"}, headers=headers,
    )
    assert response.status_code == 200
    value = response.json()
    assert value["status"] == "completed" and value["answer_complete"] is False
    assert value["completion"]["finish_reason"] == reason
    assert value["steps"][1]["completion"]["usage"]["total_tokens"] == 12
    saved = http.get(f"/api/quick-builder/runs/{value['id']}", headers=headers)
    assert saved.status_code == 200
    assert saved.json()["completion"] == value["completion"]
    assert http.get(f"/api/quick-builder/runs/{value['id']}", headers=login(http, "outsider")).status_code == 404


@pytest.mark.parametrize("state", ["complete", "stopped", "interrupted", "failed"])
def test_latest_attempt_metadata_keeps_original_stream_immutable(http, people, state):
    headers = login(http, "creator")
    created = http.post("/api/conversations", json={"title": "合成續寫狀態"}, headers=headers)
    assert created.status_code == 201
    cid = created.json()["id"]
    user = http.post(f"/api/conversations/{cid}/messages", json={"role": "user", "content": "合成問題"}, headers=headers)
    assert user.status_code == 201
    answer = http.post(f"/api/conversations/{cid}/messages", json={
        "role": "assistant", "content": "合成回答", "parent_id": user.json()["id"],
        "metadata": {"anila_stream": {"state": "complete"}},
    }, headers=headers)
    assert answer.status_code == 201
    mid = answer.json()["id"]
    changed = http.put(f"/api/conversations/{cid}/messages/{mid}", json={
        "metadata": {"response_stream_state": state},
    }, headers=headers)
    assert changed.status_code == 200
    assert changed.json()["metadata"]["response_stream_state"] == state
    assert changed.json()["metadata"]["anila_stream"] == {"state": "complete"}
    forbidden = http.put(f"/api/conversations/{cid}/messages/{mid}", json={
        "metadata": {"anila_stream": {"state": "stopped"}},
    }, headers=headers)
    assert forbidden.status_code == 409
    saved = http.get(f"/api/conversations/{cid}?view=all", headers=headers)
    assert saved.status_code == 200
    row = next(item for item in saved.json()["messages"] if item["id"] == mid)
    assert row["metadata"]["response_stream_state"] == state
    assert row["metadata"]["anila_stream"] == {"state": "complete"}


@pytest.mark.parametrize("reason", ["stop", "length", "content_filter", "tool_calls", "function_call"])
def test_memory_extracts_only_complete_model_answers(reason):
    from app.services.memory_service import turn_is_extractable

    metadata = {"anila_stream": {"state": "complete"}, "completion": result(reason=reason)["completion"]}
    assert turn_is_extractable(user_text="合成問題", assistant_text="合成回答", assistant_metadata=metadata) is (reason == "stop")


@pytest.mark.parametrize("state", ["complete", "stopped", "interrupted", "failed", "unknown", None])
def test_memory_uses_latest_local_attempt_state(state):
    from app.services.memory_service import turn_is_extractable

    metadata = {"anila_stream": {"state": "complete"}, "response_stream_state": state}
    assert turn_is_extractable(user_text="合成問題", assistant_text="合成回答", assistant_metadata=metadata) is (state == "complete")


@pytest.mark.parametrize("metadata", [{"completion": None}, {"completion": {"schema_version": 2}}, {"finish_reason": "length"}, {"finish_reason": "content_filter"}, {"answer_complete": False}])
def test_memory_rejects_invalid_or_legacy_incomplete_completion(metadata):
    from app.services.memory_service import turn_is_extractable

    assert not turn_is_extractable(user_text="合成問題", assistant_text="合成回答", assistant_metadata=metadata)
