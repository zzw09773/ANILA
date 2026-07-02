"""P0 契約：air-gap 不變式與 reasoning max_tokens 下限。

這些是「跑得起來對本地模型」與「不會偷偷外連 platform.openai.com」的分水嶺，
列為必過測試而非僅文件風險。
"""

from __future__ import annotations

import pytest
from agents import ModelSettings, OpenAIChatCompletionsModel

from anila_agent.config import ModelConfig
from anila_agent.runtime import model as model_mod

pytestmark = pytest.mark.unit


def _cfg(**kw) -> ModelConfig:
    base = dict(
        base_url="http://gpt-oss-20b:8000/v1",
        model="gpt-oss-20b",
        api_key="EMPTY",
        ssl_verify=True,
        timeout=30.0,
        settings={},
    )
    base.update(kw)
    return ModelConfig(**base)


def test_airgap_invariants_locked(monkeypatch):
    calls: dict[str, object] = {}
    monkeypatch.setattr(model_mod, "set_default_openai_api", lambda api: calls.__setitem__("api", api))
    monkeypatch.setattr(model_mod, "set_tracing_disabled", lambda d: calls.__setitem__("tracing", d))
    monkeypatch.setattr(model_mod, "_AIRGAP_LOCKED", False)

    model_mod.build_model(_cfg())

    assert calls["api"] == "chat_completions", "必須強制 Chat Completions（vLLM 不實作 Responses）"
    assert calls["tracing"] is True, "必須關閉 tracing exporter（否則外連 platform.openai.com）"


def test_build_model_is_chat_completions_object(monkeypatch):
    monkeypatch.setattr(model_mod, "_AIRGAP_LOCKED", False)
    m = model_mod.build_model(_cfg())
    # 用 OpenAIChatCompletionsModel 物件（非字串模型名）→ 繞過 Responses，也避開 GPT-5 字串預設。
    assert isinstance(m, OpenAIChatCompletionsModel)
    assert m.model == "gpt-oss-20b"
    assert "gpt-oss-20b:8000" in str(m._client.base_url)


def test_max_tokens_floored_when_too_small():
    s = model_mod.build_model_settings(_cfg(settings={"max_tokens": 64, "temperature": 0.2}))
    assert isinstance(s, ModelSettings)
    assert s.max_tokens >= model_mod.REASONING_MAX_TOKENS_FLOOR
    assert s.temperature == 0.2


def test_max_tokens_floored_when_unset():
    s = model_mod.build_model_settings(_cfg(settings={}))
    assert s.max_tokens == model_mod.REASONING_MAX_TOKENS_FLOOR


def test_larger_max_tokens_preserved():
    s = model_mod.build_model_settings(_cfg(settings={"max_tokens": 2048}))
    assert s.max_tokens == 2048


def test_json_object_settings_forces_response_format():
    # 結構化側查詢用 json_object（自架模型 json_schema 不可靠）。
    s = model_mod.json_object_settings(_cfg(settings={"max_tokens": 64, "temperature": 0.1}))
    assert s.extra_body == {"response_format": {"type": "json_object"}}
    assert s.max_tokens >= model_mod.REASONING_MAX_TOKENS_FLOOR
    assert s.temperature == 0.1
