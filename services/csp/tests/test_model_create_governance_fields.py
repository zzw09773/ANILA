# -*- coding: utf-8 -*-
"""create 也要吃得下兩個治理旗標（2026-09-19）。

病灶：router_enabled / thinking_user_selectable 先前只存在於 ModelUpdate。
UI 在「新增模型」視窗勾了它們、buildModelPayload() 也送了，但 ModelCreate
沒有這兩個欄位，pydantic 預設 extra=ignore 直接丟掉，HTTP 仍回 200 ——
列上永遠吃到 ORM 預設（false / true），對話模型選單不收
（router_model_policy._is_router_eligible 檢查 router_enabled），畫面卻說成功。
同一個缺口讓 trust-retry 那條補救路徑的授權整組漏寫。

這支守兩件事：schema 有欄 + 走真端點時真的寫進 DB。
"""
from __future__ import annotations

import asyncio
import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from pydantic import ValidationError

from app.api import models as models_api
from app.models.model_registry import ModelRegistry
from app.schemas.model_registry import ModelCreate
from tests.conftest import make_user


def _kwargs(**extra):
    base = dict(
        name="flag-model",
        display_name="Flag",
        model_type="llm",
        endpoint_url="https://gateway.example.com/v1",
    )
    base.update(extra)
    return base


def test_create_schema_carries_the_two_governance_flags():
    """欄位必須在 ModelCreate 上，否則 create 靜默丟棄（本測試是本包的存在理由）。"""
    payload = ModelCreate(**_kwargs(router_enabled=True, thinking_user_selectable=False))
    dumped = payload.model_dump()
    assert dumped["router_enabled"] is True
    assert dumped["thinking_user_selectable"] is False
    bare = ModelCreate(**_kwargs())
    # 預設值與 ORM 一致，未送時行為不變。
    assert bare.router_enabled is False
    assert bare.thinking_user_selectable is True


def test_create_schema_forbids_unknown_keys():
    """extra=forbid：下一個「UI 送了後端沒收」的欄位要在 422 現形，不是靜默。"""
    with pytest.raises(ValidationError):
        ModelCreate(**_kwargs(not_a_field=True))


def test_create_writes_the_flags_through_the_endpoint(db):
    """走真 create_model：兩個旗標必須落在列上，不在 response 裡說謊。

    直接呼叫 handler（與 test_model_gateway_hardening 同法），不經 TestClient
    —— 沙箱裡 TestClient 的 lifespan 會開 socket 卡住；這支要驗的是欄位寫入。
    """
    owner = make_user(db, "flag-owner", role="owner")
    resp = asyncio.run(
        models_api.create_model(
            ModelCreate(
                name="flag-through",
                display_name="Flag Through",
                model_type="llm",
                endpoint_url="https://gateway.example.com/v1",
                router_enabled=True,
                thinking_user_selectable=False,
            ),
            owner,
            db,
        )
    )
    assert resp["router_enabled"] is True
    assert resp["thinking_user_selectable"] is False
    row = db.query(ModelRegistry).filter(ModelRegistry.name == "flag-through").first()
    assert row is not None
    assert row.router_enabled is True
    assert row.thinking_user_selectable is False
