"""Safe service/permission error copy, using only synthetic callbacks."""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import HTTPException

from app.services.internal_llm import InternalCompletionError
from app.services.quick_flow import QuickFlowExecutionError, _error, execute_graph

CANARY = "SERVICE_ERROR_CANARY https://provider.invalid/private?api_key=SERVICE_ERROR_CANARY"


def graph(kind):
    config = {"collection_ids": [1]} if kind == "rag" else {"tool_id": 1} if kind == "mcp" else {}
    return {"nodes": [{"id": "in", "kind": "input", "config": {}}, {"id": "work", "kind": kind, "config": config}, {"id": "out", "kind": "output", "config": {}}], "edges": [{"source": "in", "target": "work"}, {"source": "work", "target": "out"}]}


async def unused(*args, **kwargs):
    raise AssertionError("unexpected callback")


def run(kind, callback):
    callbacks = {"model_call": unused, "rag_call": unused, "tool_call": unused, "document_call": unused}
    callbacks[{"model": "model_call", "rag": "rag_call", "mcp": "tool_call", "document": "document_call"}[kind]] = callback
    return asyncio.run(execute_graph(graph(kind), "合成問題", **callbacks))


@pytest.mark.parametrize("status", [None, 403, 429, 502])
def test_internal_completion_failure_is_model_service_not_guessed_permission(status):
    async def failing(*args):
        raise InternalCompletionError(status, message=CANARY)

    result = run("model", failing)
    assert result["error"]["type"] == "MODEL_SERVICE_ERROR"
    assert "模型服務未能完成" in result["error"]["message"]
    assert "權限" not in result["error"]["message"]
    assert result["steps"][-1]["error"] == result["error"]
    assert "SERVICE_ERROR_CANARY" not in json.dumps(result)


@pytest.mark.parametrize("kind,code,noun", [("model", "MODEL_SERVICE_ERROR", "模型服務"), ("rag", "RAG_SERVICE_ERROR", "文件檢索服務"), ("mcp", "TOOL_SERVICE_ERROR", "查詢工具服務"), ("document", "DOCUMENT_SERVICE_ERROR", "文件輸出服務")])
def test_unknown_callback_failures_use_known_service_without_free_message(kind, code, noun):
    async def failing(*args, **kwargs):
        raise HTTPException(403, detail=CANARY)

    result = run(kind, failing)
    assert result["status"] == "failed"
    assert result["error"]["type"] == code
    assert noun in result["error"]["message"]
    assert "授權" not in result["error"]["message"]
    assert "權限" not in result["error"]["message"]
    assert "SERVICE_ERROR_CANARY" not in json.dumps(result)


@pytest.mark.parametrize("kind,code,noun", [("model", "MODEL_SERVICE_UNAVAILABLE", "模型服務"), ("rag", "RAG_SERVICE_UNAVAILABLE", "文件檢索服務"), ("mcp", "TOOL_SERVICE_UNAVAILABLE", "查詢工具服務"), ("document", "DOCUMENT_SERVICE_UNAVAILABLE", "文件輸出服務")])
def test_missing_callback_is_service_unavailable_not_authorization(kind, code, noun):
    result = run(kind, None)
    assert result["error"]["type"] == code
    assert noun in result["error"]["message"]
    assert "授權" not in result["error"]["message"]


@pytest.mark.parametrize("kind,key,code,copy", [("model", "type", "MODEL_DENIED", "沒有這個模型的使用權限"), ("rag", "type", "SOURCE_UNAVAILABLE", "文件來源目前無法使用"), ("mcp", "type", "TOOL_UNAVAILABLE", "查詢工具目前無法使用"), ("mcp", "error_type", "mcp_tool_denied", "未核准、已停用或不在目前使用範圍"), ("rag", "error_type", "rag_collection_inactive", "文件集已停用"), ("document", "type", "EXPORT_CLASSIFICATION", "密等不允許匯出")])
def test_only_known_error_identifier_selects_permission_or_source_copy(kind, key, code, copy):
    async def failing(*args, **kwargs):
        raise HTTPException(403, detail={key: code, "message": CANARY, "action": CANARY})

    result = run(kind, failing)
    assert result["error"]["type"] == code
    assert copy in result["error"]["message"]
    assert "SERVICE_ERROR_CANARY" not in json.dumps(result)


def test_callback_execution_error_uses_fixed_copy_not_its_free_message():
    async def failing(*args, **kwargs):
        raise QuickFlowExecutionError("DOCUMENT_RENDERER_NOT_CONFIGURED", CANARY)

    result = run("document", failing)
    assert result["error"]["type"] == "DOCUMENT_RENDERER_NOT_CONFIGURED"
    assert "尚未提供這個文件輸出服務" in result["error"]["message"]
    assert "治理中心" in result["error"]["message"]
    assert "Studio" in result["error"]["message"]
    assert "登錄" in result["error"]["message"]
    assert "SERVICE_ERROR_CANARY" not in json.dumps(result)


@pytest.mark.parametrize("detail", [{"type": "UNKNOWN_DENIED", "message": CANARY}, {"type": {"x": CANARY}}, {"error_type": [CANARY]}])
def test_unknown_malformed_detail_does_not_create_permission_claim(detail):
    async def failing(*args):
        raise HTTPException(403, detail=detail)

    result = run("model", failing)
    assert result["error"]["type"] == "MODEL_SERVICE_ERROR"
    assert "權限" not in result["error"]["message"]
    assert "SERVICE_ERROR_CANARY" not in json.dumps(result)


def test_unscoped_error_is_neutral_and_masks_original_text():
    result = _error(PermissionError(CANARY))
    assert result["type"] == "PROVIDER_ERROR"
    assert "授權" not in result["message"] and "模型" not in result["message"]
    assert "SERVICE_ERROR_CANARY" not in json.dumps(result)


def test_agent_callback_permission_and_service_failures_are_not_retried():
    for error, expected in [(RuntimeError(CANARY), "TOOL_SERVICE_ERROR"), (HTTPException(403, detail={"error_type": "mcp_tool_denied", "message": CANARY}), "mcp_tool_denied")]:
        count = 0

        async def model(*args):
            return '{"action":"tool","tool_id":1,"arguments":{}}'

        async def tool(*args):
            nonlocal count
            count += 1
            raise error

        g = graph("agent")
        g["nodes"][1]["config"] = {"tool_ids": [1], "max_retries": 2}
        result = asyncio.run(execute_graph(g, "合成問題", model_call=model, rag_call=unused, tool_call=tool, document_call=unused))
        assert count == 1
        assert result["error"]["type"] == expected
        assert result["steps"][-1]["tool"][0]["error"] == result["error"]
        assert "SERVICE_ERROR_CANARY" not in json.dumps(result)


def test_explicit_tool_failed_retry_still_recovers_and_completion_usage_survives():
    calls = 0
    plans = iter(['{"action":"tool","tool_id":1,"arguments":{}}', '{"action":"final","output":"完成"}'])

    async def model(*args):
        return next(plans)

    async def tool(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise QuickFlowExecutionError("TOOL_FAILED", CANARY)
        return {"answer": "查詢完成"}

    g = graph("agent")
    g["nodes"][1]["config"] = {"tool_ids": [1], "max_retries": 2}
    result = asyncio.run(execute_graph(g, "合成問題", model_call=model, rag_call=unused, tool_call=tool, document_call=unused))
    assert result["status"] == "completed" and result["answer_complete"] is True
    assert result["completion"]["usage_complete"] is True
    assert calls == 2
    assert result["steps"][1]["tool"][0]["error"]["type"] == "TOOL_FAILED"
    assert "SERVICE_ERROR_CANARY" not in json.dumps(result)


@pytest.mark.parametrize("reason", ["stop", "length", "content_filter"])
def test_service_tag_does_not_change_completion_or_usage(reason):
    from app.services.completion_metadata import completion_metadata

    expected = completion_metadata(finish_reason=reason, terminal_source="finish_reason", usage={"prompt_tokens": 17, "completion_tokens": 9, "total_tokens": 26, "reasoning_tokens": 2}, can_continue=False)

    async def model(messages, temperature):
        return {"text": "" if reason == "content_filter" else "合成回答", "completion": expected}

    result = asyncio.run(execute_graph(graph("model"), "合成問題", model_call=model, stream_model_call=model, rag_call=unused, tool_call=unused, document_call=unused))
    assert result["status"] == "completed"
    assert result["completion"] == expected
    assert result["answer_complete"] is (reason == "stop")
    assert result["steps"][1]["model_completions"] == [expected]


def test_callback_timeout_remains_failed_service_and_cancel_still_propagates():
    async def timeout(*args):
        raise asyncio.TimeoutError(CANARY)

    timed_out = run("model", timeout)
    assert timed_out["error"]["type"] == "MODEL_SERVICE_ERROR"
    assert "SERVICE_ERROR_CANARY" not in json.dumps(timed_out)

    async def cancelled(*args):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        run("model", cancelled)


def test_agent_planning_failure_is_model_service_not_tool_failure():
    async def model(*args):
        raise InternalCompletionError(502, message=CANARY)

    g = graph("agent")
    g["nodes"][1]["config"] = {"tool_ids": [1]}
    result = asyncio.run(execute_graph(g, "合成問題", model_call=model, rag_call=unused, tool_call=unused, document_call=unused))
    assert result["error"]["type"] == "MODEL_SERVICE_ERROR"
    assert "SERVICE_ERROR_CANARY" not in json.dumps(result)


@pytest.mark.parametrize("code,copy", [("DOCUMENT_RENDERER_DENIED", "服務使用權限與密等上限"), ("DOCUMENT_RENDERER_UNAVAILABLE", "文件排版服務目前無法使用"), ("DOCUMENT_TOO_LARGE", "文件內容超過大小限制"), ("DOCUMENT_CONTEXT", "文件的來源")])
def test_document_typed_failure_is_preserved_with_fixed_safe_copy(code, copy):
    async def failing(*args, **kwargs):
        raise QuickFlowExecutionError(code, CANARY)

    result = run("document", failing)
    assert result["error"]["type"] == code
    assert copy in result["error"]["message"]
    assert "SERVICE_ERROR_CANARY" not in json.dumps(result)


@pytest.mark.parametrize("kind", ["model", "rag", "mcp", "document"])
@pytest.mark.parametrize("code", ["OUTPUT_TOO_LARGE", "OUTPUT_MASK_CLOSED", "DATA_LIMIT", "STEP_LIMIT", "RAG_CONTEXT_LIMIT"])
def test_first_party_flow_limits_keep_type_inside_every_callback(kind, code):
    async def limited(*args, **kwargs):
        raise QuickFlowExecutionError(code, CANARY)

    result = run(kind, limited)
    assert result["status"] == "failed"
    assert result["error"]["type"] == code
    assert result["steps"][-1]["error"]["type"] == code
    assert "SERVICE_ERROR_CANARY" not in json.dumps(result)


def test_unknown_first_party_identifier_is_not_reclassified_or_allowed_to_echo_free_text():
    result = _error(QuickFlowExecutionError("FUTURE_FLOW_LIMIT", CANARY), service="model")
    assert result["type"] == "FUTURE_FLOW_LIMIT"
    assert "SERVICE_ERROR_CANARY" not in json.dumps(result)


@pytest.mark.parametrize("service", [None, "model", "rag", "tool", "document"])
def test_explicit_server_cancelled_remains_cancelled_with_fixed_copy(service):
    result = _error(QuickFlowExecutionError("CANCELLED", CANARY), service=service)
    assert result["type"] == "CANCELLED"
    assert "執行已取消" in result["message"]
    assert "沒有完整答案" in result["message"]
    assert "SERVICE_ERROR_CANARY" not in json.dumps(result)
