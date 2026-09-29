"""新登錄的模型預設同時處理上限 16。明示清空仍是不限。既有列不在這裡改。"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.model_registry import ModelCreate


def _kwargs(**extra):
    base = dict(
        name="new-model",
        display_name="新模型",
        model_type="llm",
        endpoint_url="https://gateway.example.com/v1",
    )
    base.update(extra)
    return base


def test_omitted_cap_defaults_to_16_and_blank_stays_unlimited():
    assert ModelCreate(**_kwargs()).max_concurrent == 16
    assert ModelCreate(**_kwargs(max_concurrent=None)).max_concurrent is None
    assert ModelCreate(**_kwargs(max_concurrent="")).max_concurrent is None
    assert ModelCreate(**_kwargs(max_concurrent=4)).max_concurrent == 4
    with pytest.raises(ValidationError):
        ModelCreate(**_kwargs(max_concurrent=True))
