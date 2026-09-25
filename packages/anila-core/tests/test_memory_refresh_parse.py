"""閒置時一次整理：摘要不得當事實，助理的話也不得當事實。"""
from __future__ import annotations

from anila_core.memory.long_term.extraction import parse_memory_refresh_response


def test_parse_memory_refresh_keeps_summary_and_user_facts():
    raw = """
    好的。
    {"summary":"使用者要一份條列報告","facts":[
      {"key":"unit","value":"雷達組","confidence":0.9},
      {"key":"<fact_category>","value":"<concrete_value>","confidence":1}
    ]}
    """
    parsed = parse_memory_refresh_response(raw)
    assert parsed["summary"] == "使用者要一份條列報告"
    assert parsed["facts"] == [{"key": "unit", "value": "雷達組", "confidence": 0.9}]


def test_parse_memory_refresh_rejects_garbage():
    assert parse_memory_refresh_response("not json") == {"summary": "", "facts": []}
    assert parse_memory_refresh_response("[]") == {"summary": "", "facts": []}
