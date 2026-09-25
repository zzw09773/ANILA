"""整理對話時，重複的事實 key 不得弄丟摘要，失敗也不得立刻重試。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.models.platform_setting import set_setting
from app.models.user_memory import ConversationSummary, MemoryRefreshLease, UserFact
from app.services import memory_service
from tests.conftest import make_user
from tests.test_memory_review_fixes import (
    _conv,
    _install_model,
    _pair,
    _Scripted,
    _summary_role,
)

# 2026-09-25 活體：使用者這段話抽出多筆同 key 的「技術決策」。
_RADAR_TEXT = (
    "我在雷達組，負責 X 波段主動相位陣列天線。"
    "我們決定採用 GaN 功率放大器、陣列規模 16×16，散熱改用液冷。"
)
_SUMMARY = (
    "使用者在雷達組，負責 X 波段主動相位陣列天線，"
    "決定採用 GaN 功率放大器、陣列規模 16×16，散熱改用液冷。"
)


def _radar_payload() -> str:
    return json.dumps(
        {
            "summary": _SUMMARY,
            "facts": [
                {"key": "技術決策", "value": "GaN 功率放大器", "confidence": 0.9},
                {"key": "技術決策", "value": "16×16", "confidence": 0.8},
                {"key": "技術決策", "value": "液冷", "confidence": 0.7},
                {"key": "技術決策", "value": "GaN 功率放大器", "confidence": 0.4},
                {"key": "單位", "value": "雷達組", "confidence": 0.95},
            ],
        },
        ensure_ascii=False,
    )


def _due(db, user_id: int) -> list[int]:
    return memory_service.conversations_due(
        db, now=datetime.now(timezone.utc), user_id=user_id, force=False
    )


async def _idle_scan(db, user_id: int) -> None:
    """閒置迴圈對單一使用者會做的事：先列待整理，再逐則 refresh。"""
    for conversation_id in _due(db, user_id):
        await memory_service.refresh_conversation(conversation_id, db=db)


@pytest.mark.asyncio
async def test_duplicate_fact_keys_merge_and_summary_survives_embed_commit(
    db, monkeypatch
):
    """同 key 多值收成一列；_embed 的 commit 不得先寫入事實，摘要要留下。"""
    db.autoflush = False
    user = make_user(db, username="mem-dup-key")
    conv = _conv(db, user)
    _pair(db, conv, _RADAR_TEXT, "好的，已記下。")
    _summary_role(db, "summary-dup-key")
    script = _Scripted([_radar_payload()])
    _install_model(monkeypatch, script, embed=False)
    seen: dict[str, int] = {}

    async def fake_embed(session, text_input, **kwargs):
        # 正式 _embed 在打向量服務前會 commit，把連線還回池子。
        session.commit()
        seen["facts_during_embed"] = (
            session.query(UserFact).filter(UserFact.user_id == user.id).count()
        )
        seen["summaries_during_embed"] = session.query(ConversationSummary).count()
        return [1.0, 0.0], "embed-test", 2

    monkeypatch.setattr(memory_service, "_embed", fake_embed)

    await memory_service.refresh_conversation(conv.id, db=db)

    assert seen.get("facts_during_embed") == 0
    assert seen.get("summaries_during_embed") == 0
    db.expire_all()
    summary = (
        db.query(ConversationSummary)
        .filter(ConversationSummary.conversation_id == conv.id)
        .one_or_none()
    )
    assert summary is not None
    assert "雷達組" in summary.summary
    assert "GaN 功率放大器" in summary.summary
    rows = {
        row.key: row.value
        for row in db.query(UserFact).filter(UserFact.user_id == user.id)
    }
    assert set(rows) == {"技術決策", "單位"}
    assert rows["單位"] == "雷達組"
    decision = rows["技術決策"]
    assert decision.count("GaN 功率放大器") == 1
    assert decision.index("GaN 功率放大器") < decision.index("16×16")
    assert decision.index("16×16") < decision.index("液冷")
    assert len(decision) <= 1000


def test_upsert_merges_duplicate_keys_and_respects_edits_and_tombstones(db):
    """一批裡的同 key 合併後 upsert；使用者改過或已刪的列不被蓋掉。"""
    db.autoflush = False
    user = make_user(db, username="mem-merge-upsert")
    long_note = "甲" * 1200
    try:
        memory_service._upsert_facts_generic(
            db,
            user.id,
            [
                {
                    "key": "技術決策",
                    "value": "GaN 功率放大器",
                    "confidence": 0.5,
                    "evidence_message_id": 3,
                },
                {
                    "key": " 技術決策 ",
                    "value": "GaN 功率放大器",
                    "confidence": 0.9,
                    "evidence_message_id": 4,
                },
                {
                    "key": "技術決策",
                    "value": long_note,
                    "confidence": 0.2,
                    "evidence_message_id": 5,
                },
            ],
            source_conversation_id=None,
            source_message_id=5,
        )
        db.commit()
    except Exception:
        db.rollback()
    row = db.query(UserFact).filter(UserFact.user_id == user.id).one_or_none()
    assert row is not None
    assert row.key == "技術決策"
    assert row.value.count("GaN 功率放大器") == 1
    assert len(row.value) <= 1000
    assert row.confidence == 0.9

    row.user_edited = True
    row.value = "使用者改過的決策"
    db.commit()
    memory_service._upsert_facts_generic(
        db,
        user.id,
        [
            {"key": "技術決策", "value": "液冷", "confidence": 1.0, "evidence_message_id": 6},
            {"key": "技術決策", "value": "氣冷", "confidence": 1.0, "evidence_message_id": 6},
        ],
        source_conversation_id=None,
        source_message_id=6,
    )
    db.commit()
    db.refresh(row)
    assert row.value == "使用者改過的決策"

    row.user_edited = False
    row.source_message_id = 6
    db.commit()
    memory_service.remember_fact_deleted(db, row)
    db.delete(row)
    db.commit()
    memory_service._upsert_facts_generic(
        db,
        user.id,
        [
            {"key": "技術決策", "value": "液冷", "confidence": 1.0, "evidence_message_id": 6},
            {"key": "技術決策", "value": "氣冷", "confidence": 1.0, "evidence_message_id": 6},
        ],
        source_conversation_id=None,
        source_message_id=6,
    )
    db.commit()
    assert db.query(UserFact).filter(UserFact.user_id == user.id).count() == 0


@pytest.mark.asyncio
async def test_fact_write_failure_keeps_the_summary(db, monkeypatch):
    """事實寫入失敗時，已經整理好的摘要不得一起回滾。"""
    db.autoflush = False
    user = make_user(db, username="mem-fact-fail")
    conv = _conv(db, user)
    _pair(db, conv, "我在雷達組，請用條列", "好的。")
    _summary_role(db, "summary-fact-fail")
    script = _Scripted(
        [
            json.dumps(
                {
                    "summary": "使用者在雷達組，並希望用條列。",
                    "facts": [{"key": "單位", "value": "雷達組", "confidence": 0.9}],
                },
                ensure_ascii=False,
            )
        ]
    )
    _install_model(monkeypatch, script)

    def boom(*args, **kwargs):
        raise RuntimeError("fact write failed")

    monkeypatch.setattr(memory_service, "_upsert_facts_generic", boom)

    await memory_service.refresh_conversation(conv.id, db=db)

    db.expire_all()
    summary = (
        db.query(ConversationSummary)
        .filter(ConversationSummary.conversation_id == conv.id)
        .one_or_none()
    )
    assert summary is not None
    assert "雷達組" in summary.summary
    # 事實沒寫成，涵蓋範圍要退回，這段對話才會在退避後再試。
    assert summary.covered_message_id is None
    assert db.query(UserFact).filter(UserFact.user_id == user.id).count() == 0
    lease = db.get(MemoryRefreshLease, conv.id)
    assert lease is not None
    remaining = lease.claimed_until - int(datetime.now(timezone.utc).timestamp())
    assert remaining >= memory_service._REFRESH_FAILURE_BACKOFF_SECONDS - 30

    await memory_service.refresh_conversation(conv.id, db=db)
    assert script.calls == 1
    db.expire_all()
    summary = (
        db.query(ConversationSummary)
        .filter(ConversationSummary.conversation_id == conv.id)
        .one_or_none()
    )
    assert summary is not None
    assert "雷達組" in summary.summary


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["model", "embed", "db"])
async def test_failing_conversation_is_not_retried_on_the_next_idle_scan(
    failure, db, monkeypatch
):
    """模型、嵌入或資料庫失敗都要把下一輪往後推滿失敗退避，閒置掃描不得立刻重打。"""
    user = make_user(db, username=f"mem-backoff-{failure}")
    conv = _conv(db, user)
    past = datetime.now(timezone.utc) - timedelta(minutes=30)
    _pair(db, conv, "我在雷達組，請用條列", "好的。", when=past)
    _summary_role(db, f"summary-backoff-{failure}")
    set_setting(db, "memory.enabled", True)
    set_setting(db, "memory.idle_minutes", 10)
    calls = {"n": 0}

    async def boom_model(*args, **kwargs):
        calls["n"] += 1
        raise RuntimeError("model down")

    async def boom_embed(*args, **kwargs):
        raise RuntimeError("embed down")

    def boom_save(*args, **kwargs):
        raise RuntimeError("db down")

    if failure == "model":
        monkeypatch.setattr("app.services.internal_llm.complete_chat", boom_model)
    else:
        script = _Scripted(
            [
                '{"summary":"使用者在雷達組。","facts":[]}',
                '{"summary":"使用者在雷達組。","facts":[]}',
            ]
        )
        _install_model(monkeypatch, script, embed=failure != "embed")
        calls["script"] = script
        if failure == "embed":
            monkeypatch.setattr(memory_service, "_embed", boom_embed)
        else:
            monkeypatch.setattr(memory_service, "save_conversation_summary", boom_save)

    assert conv.id in _due(db, user.id)
    await _idle_scan(db, user.id)
    if failure == "model":
        assert calls["n"] == 1
    else:
        assert calls["script"].calls == 1
    assert conv.id in _due(db, user.id)
    db.expire_all()
    lease = db.get(MemoryRefreshLease, conv.id)
    assert lease is not None
    remaining = lease.claimed_until - int(datetime.now(timezone.utc).timestamp())
    assert remaining >= memory_service._REFRESH_FAILURE_BACKOFF_SECONDS - 30

    await _idle_scan(db, user.id)
    if failure == "model":
        assert calls["n"] == 1
    else:
        assert calls["script"].calls == 1
