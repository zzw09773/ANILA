"""build_agent 組裝：retriever Protocol 強制、deny-all guardrail 掛載、CITED 走 output style。"""

from __future__ import annotations

from pathlib import Path

import pytest

from anila_agent.config import AgentConfig, AppConfig, ModelConfig
from anila_agent.retrieval.dummy import DummyRetriever
from anila_agent.runtime.agent_factory import build_agent

pytestmark = pytest.mark.unit


def _cfg(tmp_path: Path) -> AppConfig:
    return AppConfig(
        model=ModelConfig(
            base_url="http://x/v1", model="gpt-oss-20b", api_key="EMPTY", ssl_verify=True, timeout=30.0
        ),
        agent=AgentConfig(name="t-agent", max_turns=7),
        home=tmp_path,
        log_level="INFO",
    )


def test_rejects_non_protocol_retriever(tmp_path):
    with pytest.raises(TypeError):
        build_agent(_cfg(tmp_path), retriever=object())


def test_assembles_with_policy_guardrails(tmp_path, monkeypatch):
    monkeypatch.delenv("ANILA_MEMORY", raising=False)
    monkeypatch.delenv("ANILA_CITED", raising=False)
    a = build_agent(_cfg(tmp_path), retriever=DummyRetriever())
    assert a.max_turns == 7
    assert a.context.memory is None  # memdir 預設關
    assert [t.name for t in a.agent.tools] == ["search_documents", "read_document"]
    # 每個工具都掛上 deny-all 政策 guardrail。
    assert all(t.tool_input_guardrails for t in a.agent.tools)
    assert a.agent.output_type is None  # 預設無結構化 output_type


def test_memdir_adds_search_memory_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("ANILA_MEMORY", "1")
    a = build_agent(_cfg(tmp_path), retriever=DummyRetriever())
    assert a.context.memory is not None
    assert "search_memory" in [t.name for t in a.agent.tools]


def test_cited_uses_output_style_not_output_type(tmp_path, monkeypatch):
    monkeypatch.delenv("ANILA_MEMORY", raising=False)
    monkeypatch.setenv("ANILA_CITED", "1")
    a = build_agent(_cfg(tmp_path), retriever=DummyRetriever())
    # 不掛 SDK output_type（自架模型 json_schema+tools 不可靠），改 prompt 行內引用。
    assert a.agent.output_type is None
    assert "來源" in a.agent.instructions
