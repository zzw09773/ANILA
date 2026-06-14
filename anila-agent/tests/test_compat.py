"""strict 欄位剝除（自架 OpenAI-compatible 端點相容）。"""

from __future__ import annotations

import json

import pytest

from anila_agent.runtime.compat import strip_strict_fields

pytestmark = pytest.mark.unit


def test_strips_tool_function_strict():
    body = json.dumps(
        {"model": "m", "tools": [{"type": "function", "function": {"name": "f", "strict": True, "parameters": {}}}]}
    ).encode()
    data = json.loads(strip_strict_fields(body))
    assert "strict" not in data["tools"][0]["function"]
    assert data["tools"][0]["function"]["name"] == "f"  # 其他欄位保留


def test_strips_strict_false_too():
    # 重點：strict:false 也要剝（端點是對欄位存在報錯，非對值）。
    body = json.dumps({"tools": [{"type": "function", "function": {"name": "f", "strict": False}}]}).encode()
    assert "strict" not in json.loads(strip_strict_fields(body))["tools"][0]["function"]


def test_strips_response_format_json_schema_strict():
    body = json.dumps(
        {"response_format": {"type": "json_schema", "json_schema": {"name": "x", "strict": True, "schema": {}}}}
    ).encode()
    assert "strict" not in json.loads(strip_strict_fields(body))["response_format"]["json_schema"]


def test_no_strict_returns_none():
    assert strip_strict_fields(json.dumps({"model": "m", "messages": []}).encode()) is None


def test_invalid_body_returns_none():
    assert strip_strict_fields(b"not json at all") is None


def test_preserves_unrelated_fields():
    body = json.dumps(
        {"model": "m", "temperature": 0.3, "tools": [{"type": "function", "function": {"name": "f", "strict": True}}]}
    ).encode()
    data = json.loads(strip_strict_fields(body))
    assert data["temperature"] == 0.3 and data["model"] == "m"
