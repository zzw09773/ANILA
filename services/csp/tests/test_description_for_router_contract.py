# -*- coding: utf-8 -*-
"""description_for_router 契約：一行能力描述，擋 system-prompt bleed。

只驗 API／schema（Pydantic）。make_agent() 直接寫 DB 的路徑刻意不改、也不在此測。
"""

from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from pydantic import ValidationError

from app.api.agents.registration import AgentRegisterRequest, AgentUpdateRequest
from app.schemas.contracts.agents import (
    DESCRIPTION_FOR_ROUTER_EXAMPLE,
    DESCRIPTION_FOR_ROUTER_MAX_CHARS,
    AgentManifest,
    validate_description_for_router,
)

# 活體法律 agent 把整份 system prompt 貼進 description_for_router 的形狀（含換行）。
_LIVE_LEGAL_PROMPT = (
    "你是一個專業的法律文件助理，負責協助使用者查詢與解釋中華民國法律條文、"
    "行政函釋與院內規章。\n"
    "【核心原則】回答必須有依據。\n"
    "【範圍判斷】本系統僅提供法律文件檢索與條文解釋服務；"
    "無法回答與法律無關的問題。不在本系統服務範圍的詢問應予拒絕。\n"
    "DISPATCH:iso42001-probe:請查詢民法"
)

# 同一段指令壓成單行、且 ≤200 字，用來釘 bleed 黑名單（不是換行／超長先擋）。
_BLEED_ONELINE = (
    "你是一個專業的法律文件助理。本系統僅提供法律文件檢索與條文解釋；"
    "無法回答與法律無關的問題。不在本系統服務範圍。"
)


def _register(**overrides) -> AgentRegisterRequest:
    payload = {
        "name": "probe-agent",
        "endpoint_url": "http://agent:9100",
        "description_for_router": "Handles HR queries",
        "base_model_id": 1,
    }
    payload.update(overrides)
    return AgentRegisterRequest.model_validate(payload)


def _manifest(**overrides) -> AgentManifest:
    payload = {
        "agent_id": "risk-agent",
        "name": "風險分析 Agent",
        "version": "1.0.0",
        "runtime_type": "langchain",
        "classification": {"ceiling": "機密", "default": "營業秘密"},
    }
    payload.update(overrides)
    return AgentManifest.model_validate(payload)


def _error_text(exc: ValidationError) -> str:
    return " ".join(err.get("msg", "") for err in exc.errors())


def test_short_capability_descriptions_pass():
    assert validate_description_for_router("Handles HR queries") == "Handles HR queries"
    assert (
        validate_description_for_router(DESCRIPTION_FOR_ROUTER_EXAMPLE)
        == DESCRIPTION_FOR_ROUTER_EXAMPLE
    )
    _register(description_for_router="Handles HR queries")
    _register(description_for_router="檢索員工請假與差旅報支規定")
    _register(description_for_router="x")


def test_strips_surrounding_whitespace():
    got = validate_description_for_router("  Handles HR queries  ")
    assert got == "Handles HR queries"


def test_rejects_newlines():
    with pytest.raises(ValueError, match="不可含換行"):
        validate_description_for_router("Handles HR\nqueries")
    with pytest.raises(ValidationError) as exc:
        _register(description_for_router="Handles HR\rqueries")
    assert "不可含換行" in _error_text(exc.value)
    assert DESCRIPTION_FOR_ROUTER_EXAMPLE in _error_text(exc.value)


def test_rejects_over_max_chars():
    ok = "a" * DESCRIPTION_FOR_ROUTER_MAX_CHARS
    assert validate_description_for_router(ok) == ok
    too_long = "a" * (DESCRIPTION_FOR_ROUTER_MAX_CHARS + 1)
    with pytest.raises(ValidationError) as exc:
        _register(description_for_router=too_long)
    text = _error_text(exc.value)
    assert "最多 200 字元" in text
    assert DESCRIPTION_FOR_ROUTER_EXAMPLE in text


def test_rejects_live_legal_system_prompt():
    with pytest.raises(ValidationError) as exc:
        _register(description_for_router=_LIVE_LEGAL_PROMPT)
    text = _error_text(exc.value)
    assert DESCRIPTION_FOR_ROUTER_EXAMPLE in text
    assert "不可含換行" in text or "不可貼上 system prompt" in text or "最多 200 字元" in text


def test_rejects_collapsed_instruction_bleed():
    with pytest.raises(ValidationError) as exc:
        _register(description_for_router=_BLEED_ONELINE)
    text = _error_text(exc.value)
    assert "不可貼上 system prompt" in text
    assert DESCRIPTION_FOR_ROUTER_EXAMPLE in text


def test_register_empty_string_is_422_shape():
    with pytest.raises(ValidationError) as exc:
        _register(description_for_router="   ")
    assert "不可為空白" in _error_text(exc.value)


def test_update_none_skips_validation():
    req = AgentUpdateRequest.model_validate({"endpoint_url": "http://agent:9100"})
    assert req.description_for_router is None
    req = AgentUpdateRequest.model_validate({"description_for_router": None})
    assert req.description_for_router is None


def test_update_empty_string_rejected():
    with pytest.raises(ValidationError) as exc:
        AgentUpdateRequest.model_validate({"description_for_router": ""})
    assert "不可為空白" in _error_text(exc.value)


def test_update_bleed_prompt_rejected():
    with pytest.raises(ValidationError) as exc:
        AgentUpdateRequest.model_validate(
            {"description_for_router": _BLEED_ONELINE}
        )
    assert "不可貼上 system prompt" in _error_text(exc.value)


def test_manifest_empty_is_allowed():
    m = _manifest()
    assert m.description_for_router == ""
    m = _manifest(description_for_router="")
    assert m.description_for_router == ""


def test_manifest_filled_bleed_is_rejected():
    with pytest.raises(ValidationError) as exc:
        _manifest(description_for_router=_BLEED_ONELINE)
    assert "不可貼上 system prompt" in _error_text(exc.value)


def test_manifest_filled_capability_line_passes():
    m = _manifest(description_for_router="Handles HR queries")
    assert m.description_for_router == "Handles HR queries"
