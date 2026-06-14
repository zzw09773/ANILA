"""reasoning-model 防禦性 JSON 解析：永不丟例外，失敗一律 fail-closed。"""

from __future__ import annotations

import pytest

from anila_agent.util.structured import parse_json_object, strip_fences

pytestmark = pytest.mark.unit


def test_plain_json():
    assert parse_json_object('{"names": ["a", "b"]}') == {"names": ["a", "b"]}


def test_fenced_json_gemma_style():
    assert parse_json_object('```json\n{"names": ["a"]}\n```') == {"names": ["a"]}


def test_bare_fence():
    assert parse_json_object("```\n{\"x\": 1}\n```") == {"x": 1}


def test_none_fails_closed():
    assert parse_json_object(None) == {}


def test_empty_or_whitespace_fails_closed():
    # 典型 reasoning 截斷結果：content=None 或空白。
    assert parse_json_object("") == {}
    assert parse_json_object("   \n  ") == {}


def test_garbage_fails_closed():
    assert parse_json_object("這不是 JSON") == {}


def test_array_is_not_object_fails_closed():
    assert parse_json_object("[1, 2, 3]") == {}


def test_default_returned_on_failure():
    assert parse_json_object(None, {"names": []}) == {"names": []}


def test_embedded_object_extracted():
    assert parse_json_object('結果如下：{"x": 1} 完畢') == {"x": 1}


def test_strip_fences_passthrough():
    assert strip_fences("no fence here") == "no fence here"
    assert strip_fences("```json\n{}\n```") == "{}"
