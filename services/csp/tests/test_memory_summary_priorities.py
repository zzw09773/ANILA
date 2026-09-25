"""活體：摘要改去記助理的三點建議，使用者自己的技術決定消失。"""
from __future__ import annotations

import json

import pytest

from app.models.user_memory import ConversationSummary, UserFact
from app.services import memory_service
from tests.conftest import make_user
from tests.test_memory_review_fixes import (
    _conv,
    _install_model,
    _pair,
    _Scripted,
    _summary_role,
)

# 使用者原話。決定是 GaN、16×16、液冷；請求才是「列出三個重點」。
_USER_TEXT = (
    "我在雷達組，負責 X 波段主動相位陣列天線。"
    "我們決定採用 GaN 功率放大器、陣列規模 16×16，散熱改用液冷。"
    "請幫我列出液冷散熱設計要注意的三個重點"
)
# 助理的建議。摘要最多帶過，不可取代上面的決定。
_ASSISTANT_TEXT = (
    "可以從三方面著手。第一，先做熱路徑上的熱阻分配，確認接面溫升留有餘裕。"
    "第二，冷板內的流路要讓各通道流量接近，避免角落堆積熱量。"
    "第三，選液時要核對密封件與金屬是否會被侵蝕，並留下更換週期。"
    "需要的話可以再改成檢查清單。"
)
_SUMMARY = (
    "使用者的目標是列出液冷散熱設計要注意的三個重點。"
    "使用者決定採用 GaN 功率放大器、陣列規模 16×16，散熱改用液冷，"
    "人在雷達組，負責 X 波段主動相位陣列天線。"
    "助理用一句話帶過注意方向。"
)
# 摘要優先順序。前者必須出現在後者之前。
_SUMMARY_PRIORITIES = (
    "使用者的目標",
    "決定與限制",
    "零件名稱、尺寸、數字",
    "未解問題或下一步",
    "一句話帶過",
)


def _assert_priorities(prompt: str) -> None:
    positions = []
    for phrase in _SUMMARY_PRIORITIES:
        assert phrase in prompt
        positions.append(prompt.index(phrase))
    assert positions == sorted(positions)
    assert "600" in prompt
    assert "專案技術選擇" in prompt
    assert "使用者親口" in prompt


def _compliant_payload() -> str:
    return json.dumps(
        {
            "summary": _SUMMARY,
            "facts": [
                {"key": "單位", "value": "雷達組", "confidence": 0.95},
                {
                    "key": "職責",
                    "value": "負責 X 波段主動相位陣列天線",
                    "confidence": 0.9,
                },
                {
                    "key": "專案技術選擇",
                    "value": "GaN 功率放大器、陣列規模 16×16，散熱改用液冷",
                    "confidence": 0.9,
                },
            ],
        },
        ensure_ascii=False,
    )


@pytest.mark.asyncio
async def test_refresh_prompt_keeps_user_decisions_ahead_of_assistant_advice(
    db, monkeypatch
):
    """整理這段對話時，送出的提示要按順序要求留下具體決定。

    模型回傳合規 JSON 後，GaN、16×16、液冷要進摘要與「專案技術選擇」，
    不可只剩下單位與職責。
    """
    user = make_user(db, username="mem-summary-priority")
    conv = _conv(db, user)
    user_msg, _asst = _pair(db, conv, _USER_TEXT, _ASSISTANT_TEXT)
    _summary_role(db, "summary-priority")
    script = _Scripted([_compliant_payload()])
    _install_model(monkeypatch, script)

    await memory_service.refresh_conversation(conv.id, db=db)

    assert script.calls == 1
    payload = script.seen[0]
    system_prompt = payload["messages"][0]["content"]
    transcript = payload["messages"][1]["content"]
    _assert_priorities(system_prompt)
    _assert_priorities(memory_service._REFRESH_RETRY_NOTE)
    assert _USER_TEXT in transcript
    assert "這不是使用者的決定" in transcript

    db.expire_all()
    stored = (
        db.query(ConversationSummary)
        .filter(ConversationSummary.conversation_id == conv.id)
        .one()
    )
    assert len(stored.summary) <= 600
    assert "GaN 功率放大器" in stored.summary
    assert "16×16" in stored.summary
    assert "液冷" in stored.summary
    assert "熱阻分配" not in stored.summary
    rows = {
        row.key: row.value
        for row in db.query(UserFact).filter(UserFact.user_id == user.id)
    }
    assert rows["單位"] == "雷達組"
    assert rows["職責"] == "負責 X 波段主動相位陣列天線"
    choice = rows["專案技術選擇"]
    assert "GaN 功率放大器" in choice
    assert "16×16" in choice
    assert "液冷" in choice
    assert "熱阻" not in choice
    fact = (
        db.query(UserFact)
        .filter(UserFact.user_id == user.id, UserFact.key == "專案技術選擇")
        .one()
    )
    assert fact.source_message_id == user_msg.id
