"""閒置時一次整理：摘要不得當事實，助理的話也不得當事實。"""
from __future__ import annotations

from anila_core.memory.long_term.extraction import (
    EXTRACTION_SYSTEM_PROMPT,
    MEMORY_REFRESH_SYSTEM_PROMPT,
    parse_memory_refresh_response,
)


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


def test_extraction_prompts_ask_for_one_value_per_distinct_key():
    """同一類別多個值時，模型不得重複同一個籠統 key。

    活體上「技術決策」被重複使用，寫入撞 (user_id, key)。提示要指定具體
    key，並且一個 key 只放一個 value。
    """
    for prompt in (MEMORY_REFRESH_SYSTEM_PROMPT, EXTRACTION_SYSTEM_PROMPT):
        assert "單位" in prompt
        assert "職責" in prompt
        assert "專案技術選擇" in prompt
        assert "每個 key 只出現一次" in prompt
        assert "一個 value" in prompt


# 活體原話。摘要當時只留下「列出三個重點」與助理的熱阻、流道、相容性建議。
_RADAR_DECISION = "GaN 功率放大器、陣列規模 16×16，散熱改用液冷"


def test_parse_memory_refresh_keeps_concrete_decision_values():
    """模型交回合規 JSON 時，零件名稱、尺寸、數字要原樣留下。

    佔位符仍要丟掉。具體值不是模板，不可因含有英文或乘號被清掉。
    """
    raw = f"""
    思考完畢。
    {{"summary":"使用者的目標是列出液冷散熱設計要注意的三個重點。使用者決定採用 {_RADAR_DECISION}。","facts":[
      {{"key":"單位","value":"雷達組","confidence":0.95}},
      {{"key":"職責","value":"負責 X 波段主動相位陣列天線","confidence":0.9}},
      {{"key":"專案技術選擇","value":"GaN 功率放大器","confidence":0.9}},
      {{"key":"專案技術選擇","value":"陣列規模 16×16","confidence":0.8}},
      {{"key":"專案技術選擇","value":"散熱改用液冷","confidence":0.85}},
      {{"key":"<類別>","value":"<使用者原話裡的內容>","confidence":1}}
    ]}}
    """
    parsed = parse_memory_refresh_response(raw)
    assert "GaN 功率放大器" in parsed["summary"]
    assert "16×16" in parsed["summary"]
    assert "液冷" in parsed["summary"]
    assert [fact["value"] for fact in parsed["facts"] if fact["key"] == "單位"] == [
        "雷達組"
    ]
    assert [fact["value"] for fact in parsed["facts"] if fact["key"] == "職責"] == [
        "負責 X 波段主動相位陣列天線"
    ]
    choices = [
        fact["value"] for fact in parsed["facts"] if fact["key"] == "專案技術選擇"
    ]
    assert choices == ["GaN 功率放大器", "陣列規模 16×16", "散熱改用液冷"]
    assert all(not fact["value"].startswith("<") for fact in parsed["facts"])

    # 提示要把這些具體值寫進摘要與事實，否則模型會只記助理的建議。
    for prompt in (MEMORY_REFRESH_SYSTEM_PROMPT, EXTRACTION_SYSTEM_PROMPT):
        assert "零件名稱、尺寸、數字" in prompt
        assert "專案技術選擇" in prompt
        assert "助理說的話不是事實" in prompt
