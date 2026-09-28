"""事實只在與這一輪問題相近時注入；偏好每一輪都在。"""
from __future__ import annotations

import logging

import pytest

from app.models.platform_setting import set_setting
from app.models.user_memory import UserFact
from app.services import memory_service
from tests.conftest import make_user


def _fact(fact_id: int, user_id: int, key: str, value: str) -> UserFact:
    return UserFact(
        id=fact_id,
        user_id=user_id,
        key=key,
        value=value,
        confidence=1.0,
    )


def _install_embed(monkeypatch, mapping):
    """依文字回固定向量。沒對到的句子用正交向量，避免誤判成相關。"""

    async def fake_embed(_db, text_input, **kwargs):
        for needle, vector in mapping:
            if needle in (text_input or ""):
                return vector, "embed-test", len(vector)
        return [0.0, 1.0], "embed-test", 2

    monkeypatch.setattr(memory_service, "_embed", fake_embed)


@pytest.mark.asyncio
async def test_irrelevant_fact_stays_out_and_preference_stays_in(db, monkeypatch):
    """問液冷手冊時，不相干的單位事實不得進提示；語氣偏好仍要在。"""
    user = make_user(db, username="mem-rel")
    db.add(_fact(1, user.id, "unit", "雷達組"))
    db.add(_fact(2, user.id, "preference.tone", "請用條列"))
    set_setting(db, "memory.retrieve_min_cosine", 0.4)
    set_setting(db, "memory.retrieve_top_k", 3)
    db.commit()
    _install_embed(
        monkeypatch,
        (
            ("液冷", [0.0, 1.0]),
            ("雷達組", [1.0, 0.0]),
        ),
    )

    result = await memory_service.build_memory_block(
        db, user.id, "請寫一份液冷散熱系統維護手冊"
    )
    block = result.block or ""
    assert "雷達組" not in block
    assert "請用條列" in block


@pytest.mark.asyncio
async def test_relevant_fact_is_injected(db, monkeypatch):
    """與問題同向的事實要注入；正交的另一筆不要。"""
    user = make_user(db, username="mem-hit")
    db.add(_fact(3, user.id, "unit", "雷達組"))
    db.add(_fact(4, user.id, "project", "AESA 陣列"))
    set_setting(db, "memory.retrieve_min_cosine", 0.4)
    set_setting(db, "memory.retrieve_top_k", 3)
    db.commit()
    _install_embed(
        monkeypatch,
        (
            ("雷達組編制", [1.0, 0.0]),
            ("雷達組", [1.0, 0.0]),
            ("AESA", [0.0, 1.0]),
        ),
    )

    result = await memory_service.build_memory_block(db, user.id, "雷達組編制是什麼")
    block = result.block or ""
    assert "雷達組" in block
    assert "AESA" not in block


@pytest.mark.asyncio
async def test_embedding_failure_injects_preferences_only(db, monkeypatch, caplog):
    """嵌入角色沒設或呼叫失敗時，只留偏好，聊天不能因此失敗。同一原因只記一次。"""
    user = make_user(db, username="mem-embed-down")
    db.add(_fact(5, user.id, "unit", "雷達組"))
    db.add(_fact(6, user.id, "preference.tone", "請用條列"))
    db.commit()

    async def boom(_db, _text, **_kwargs):
        raise RuntimeError("no platform embedding model")

    monkeypatch.setattr(memory_service, "_embed", boom)
    memory_service.reset_fact_embed_warning()
    caplog.set_level(logging.WARNING, logger="app.services.memory_service")

    first = await memory_service.build_memory_block(db, user.id, "液冷散熱")
    second = await memory_service.build_memory_block(db, user.id, "再問一次")
    assert "雷達組" not in (first.block or "")
    assert "請用條列" in (first.block or "")
    assert "雷達組" not in (second.block or "")
    warnings = [
        rec.getMessage()
        for rec in caplog.records
        if rec.levelno >= logging.WARNING and "embed" in rec.getMessage().lower()
    ]
    assert len(warnings) == 1


@pytest.mark.asyncio
async def test_fact_count_respects_retrieve_top_k(db, monkeypatch):
    """過門檻的事實最多取 memory.retrieve_top_k 筆，偏好不占這個名額。"""
    user = make_user(db, username="mem-topk")
    db.add(_fact(7, user.id, "preference.tone", "請用條列"))
    db.add(_fact(8, user.id, "a", "FACT_A"))
    db.add(_fact(9, user.id, "b", "FACT_B"))
    db.add(_fact(10, user.id, "c", "FACT_C"))
    set_setting(db, "memory.retrieve_top_k", 1)
    set_setting(db, "memory.retrieve_min_cosine", 0.4)
    db.commit()

    async def same(_db, _text, **_kwargs):
        return [1.0, 0.0], "embed-test", 2

    monkeypatch.setattr(memory_service, "_embed", same)
    result = await memory_service.build_memory_block(db, user.id, "任何問題")
    block = result.block or ""
    assert "請用條列" in block
    present = [name for name in ("FACT_A", "FACT_B", "FACT_C") if name in block]
    assert len(present) == 1


@pytest.mark.asyncio
async def test_missing_fact_embedding_is_filled_on_retrieval(db, monkeypatch):
    """舊事實沒有向量時，第一次取用就補上，而且補完的向量要能拿來比對。"""
    user = make_user(db, username="mem-backfill")
    db.add(_fact(11, user.id, "unit", "雷達組"))
    set_setting(db, "memory.retrieve_min_cosine", 0.4)
    db.commit()

    async def fake_embed(_db, text_input, **kwargs):
        if kwargs.get("embedding_input_role") == "document" or "雷達組" in (text_input or ""):
            return [1.0, 0.0], "embed-test", 2
        return [1.0, 0.0], "embed-test", 2

    monkeypatch.setattr(memory_service, "_embed", fake_embed)
    result = await memory_service.build_memory_block(db, user.id, "雷達組在哪")
    assert "雷達組" in (result.block or "")
    stored = getattr(
        db.query(UserFact).filter(UserFact.key == "unit").one(),
        "embedding",
        None,
    )
    assert stored
