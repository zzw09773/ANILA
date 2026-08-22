# -*- coding: utf-8 -*-
"""ANILALM 不用密等——分類 **核心** 把關（Q59，幕僚長 2026-08-22 裁「閘下移核心」）。

不變式（不是處方）：``origin='anilalm'`` 的資源，**任何路徑**都不得取得高於
無機密的等級——不是「這幾條路擋住了」。

把關點 = ``apply_classification`` 核心內的一格。任何路徑（手動 classify、
collection 升密、proxy 的 agent_policy／memory_inherited latch、**未來新寫的
呼叫點**）要寫入高於無機密的等級，都過同一格。此檔驗：

1. **核心直呼**＝「新增一條路徑，守衛會不會紅」：直接
   ``apply_classification`` 一個 anilalm 對話 → 403、等級不變。
2. **三條既有 latch 逐條**（proxy agent_policy／proxy memory_inherited／
   conversation_service agent_policy）→ 各自 403、等級不變。
3. **反向**：origin 非 anilalm 的對話，核心直呼升密照樣成功。
"""

from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from fastapi import HTTPException

from app.models.conversation import Conversation
from app.modules.policy import apply_classification
from app.schemas.contracts.classification import ClassificationLevel
from tests.conftest import make_agent, make_user


def _make_conv(db, user, *, origin: str | None) -> Conversation:
    conv = Conversation(user_id=user.id, title=f"ga-{origin}", origin=origin)
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


# ── 1. 核心直呼（新增一條路也會紅）───────────────────────────────────────


def test_core_refuses_anilalm_conversation_direct_call(db):
    """直接呼叫核心把一個 anilalm 對話升密 → 403，等級不變。

    這格的價值正是「新增一條呼叫核心的路徑，守衛會不會紅」——它不經任何
    API 端點／latch，純呼叫 apply_classification。缺核心閘時這格恆綠
    （真的寫成密），修好才 403。
    """
    user = make_user(db, username="core_ga_direct")
    conv = _make_conv(db, user, origin="anilalm")

    with pytest.raises(HTTPException) as excinfo:
        apply_classification(
            db,
            resource_type="conversation",
            resource_id=str(conv.id),
            new_level="密",
            actor_type="service",
            actor_id="test-caller",
            reason="agent_policy",
        )
    assert excinfo.value.status_code == 403

    db.expire_all()
    row = db.get(Conversation, conv.id)
    assert row.classification_level == ClassificationLevel.UNCLASSIFIED.value
    assert row.classified is False


def test_core_refuses_anilalm_conversation_trade_secret(db):
    """全擋：營業秘密也拿不到（門檻＝任何高於無機密）。"""
    user = make_user(db, username="core_ga_ts")
    conv = _make_conv(db, user, origin="anilalm")

    with pytest.raises(HTTPException):
        apply_classification(
            db,
            resource_type="conversation",
            resource_id=str(conv.id),
            new_level="營業秘密",
            actor_type="service",
            actor_id="test-caller",
            reason="agent_policy",
        )
    db.expire_all()
    row = db.get(Conversation, conv.id)
    assert row.classification_level == ClassificationLevel.UNCLASSIFIED.value


# ── 2. 三條既有 latch 逐條 ─────────────────────────────────────────────


def test_proxy_agent_policy_latch_refused(db):
    from app.api.proxy import _latch_agent_classification

    user = make_user(db, username="core_ga_proxy_agent")
    conv = _make_conv(db, user, origin="anilalm")
    with pytest.raises(HTTPException):
        _latch_agent_classification(db, conv.id, "密")
    db.expire_all()
    assert db.get(Conversation, conv.id).classification_level == "無機密"


def test_proxy_memory_inherited_latch_refused(db):
    from app.api.proxy import _latch_inherited_classification

    user = make_user(db, username="core_ga_proxy_mem")
    conv = _make_conv(db, user, origin="anilalm")
    with pytest.raises(HTTPException):
        _latch_inherited_classification(db, conv.id)
    db.expire_all()
    assert db.get(Conversation, conv.id).classification_level == "無機密"


def test_service_agent_policy_latch_refused(db):
    from app.services.conversation_service import _latch_agent_policy_on_conversation

    user = make_user(db, username="core_ga_svc_agent")
    agent = make_agent(db, user, name="core-ga-encrypted-agent")
    # 讓有效政策等級高於無機密（否則 latch 在 helper 內 early-return）。
    agent.requires_encryption = True
    db.commit()
    db.refresh(agent)
    conv = _make_conv(db, user, origin="anilalm")
    with pytest.raises(HTTPException):
        _latch_agent_policy_on_conversation(db, conv.id, agent)
    db.expire_all()
    assert db.get(Conversation, conv.id).classification_level == "無機密"


# ── 3. 反向：非 anilalm 資源照樣升 ─────────────────────────────────────


def test_core_allows_non_anilalm_conversation(db):
    """origin 非 anilalm（None＝legacy ANILA）的對話，核心直呼升密照樣成功。"""
    user = make_user(db, username="core_ga_reverse")
    conv = _make_conv(db, user, origin=None)

    ev = apply_classification(
        db,
        resource_type="conversation",
        resource_id=str(conv.id),
        new_level="密",
        actor_type="service",
        actor_id="test-caller",
        reason="agent_policy",
    )
    assert ev is not None
    db.expire_all()
    row = db.get(Conversation, conv.id)
    assert row.classification_level == "密"
    assert row.classified is True


def test_core_allows_csp_conversation(db):
    user = make_user(db, username="core_ga_reverse_csp")
    conv = _make_conv(db, user, origin="csp")
    apply_classification(
        db,
        resource_type="conversation",
        resource_id=str(conv.id),
        new_level="機密",
        actor_type="service",
        actor_id="test-caller",
        reason="agent_policy",
    )
    db.expire_all()
    assert db.get(Conversation, conv.id).classification_level == "機密"
