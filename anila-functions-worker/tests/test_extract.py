"""Tests for the static AST extractor."""

from __future__ import annotations

from sandbox.extract import static_extract


def test_pure_literal_actions_no_dynamic_needed() -> None:
    code = '''
"""title: Hello
version: 1.0
"""
class Action:
    actions = [{"id": "btn", "name": "Btn", "icon_url": None}]

    async def action(self, body, **kw): pass
'''
    result = static_extract(code)
    assert result["errors"] == []
    assert result["needs_dynamic"] is False
    assert result["actions"] == [
        {"id": "btn", "name": "Btn", "icon_url": None}
    ]
    assert result["metadata"]["title"] == "Hello"
    assert result["metadata"]["version"] == "1.0"
    assert result["strategy"] == "ast"


def test_dynamic_actions_flag_triggers_stage2() -> None:
    code = '''
class Action:
    actions = [{"id": f"btn-{i}", "name": "x"} for i in range(3)]

    async def action(self, body, **kw): pass
'''
    result = static_extract(code)
    assert result["needs_dynamic"] is True
    assert result["strategy"] == "ast+sandbox"


def test_simple_valves_schema_extracted() -> None:
    code = '''
from pydantic import BaseModel

class Valves(BaseModel):
    api_endpoint: str
    threshold: int
    enabled: bool

class Action:
    actions = [{"id": "x", "name": "X"}]
'''
    result = static_extract(code)
    assert result["needs_dynamic"] is False
    schema = result["valves_schema"]
    assert schema["properties"]["api_endpoint"] == {"type": "string"}
    assert schema["properties"]["threshold"] == {"type": "integer"}
    assert schema["properties"]["enabled"] == {"type": "boolean"}


def test_complex_valves_falls_through_to_dynamic() -> None:
    """Custom types or Annotated[] hints aren't supported by the AST
    walker — flag as dynamic so stage 2 can pull the real schema.
    """
    code = '''
from typing import Annotated
from pydantic import BaseModel, Field

class Valves(BaseModel):
    api_token: Annotated[str, Field(json_schema_extra={"secret": True})]

class Action:
    actions = [{"id": "x", "name": "X"}]
'''
    result = static_extract(code)
    assert result["needs_dynamic"] is True


def test_syntax_error_returns_error_no_crash() -> None:
    result = static_extract("def bad(:")
    assert result["errors"]
    assert result["actions"] == []


def test_no_action_class_returns_empty_no_error() -> None:
    """Caller decides whether missing Action is fatal. The static
    extractor just reports what's there.
    """
    result = static_extract("x = 1")
    assert result["errors"] == []
    assert result["actions"] == []
