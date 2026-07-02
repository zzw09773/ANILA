"""retriever 自動選擇優先序 + partial-config raise 歸因（平台契約）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from anila_agent.config import AgentConfig, AppConfig, ModelConfig
from anila_agent.retrieval.anila_pgvector import AnilaPgVectorRetriever
from anila_agent.retrieval.csp_http import CspHttpRetriever
from anila_agent.retrieval.dummy import DummyRetriever
from anila_agent.retrieval.from_env import select_retriever

pytestmark = pytest.mark.unit

_RETRIEVER_ENV = [
    "ANILA_CSP_BASE_URL", "ANILA_COLLECTION_ID", "ANILA_CSP_API_KEY", "ANILA_API_KEY",
    "ANILA_BASE_URL", "PGVECTOR_URL", "PGVECTOR_COLLECTION",
    "ANILA_EMBED_BASE_URL", "ANILA_EMBED_API_KEY",
]


@pytest.fixture
def clean_env(monkeypatch):
    for name in _RETRIEVER_ENV:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _cfg() -> AppConfig:
    return AppConfig(
        model=ModelConfig(
            base_url="http://x/v1", model="m", api_key="EMPTY", ssl_verify=True, timeout=30.0
        ),
        agent=AgentConfig(name="t", max_turns=10),
        home=Path(".anila"),
        log_level="INFO",
    )


def test_no_env_falls_back_to_dummy(clean_env):
    assert isinstance(select_retriever(_cfg()), DummyRetriever)


def test_csp_selected_when_base_and_collection_set(clean_env):
    clean_env.setenv("ANILA_CSP_BASE_URL", "https://csp")
    clean_env.setenv("ANILA_COLLECTION_ID", "2")
    clean_env.setenv("ANILA_API_KEY", "csk-x")
    assert isinstance(select_retriever(_cfg()), CspHttpRetriever)


def test_anila_pgvector_selected_without_csp_base(clean_env):
    clean_env.setenv("ANILA_COLLECTION_ID", "2")
    clean_env.setenv("PGVECTOR_URL", "postgresql://u@h/db")
    clean_env.setenv("ANILA_BASE_URL", "http://e/v1")
    clean_env.setenv("ANILA_API_KEY", "k")
    assert isinstance(select_retriever(_cfg()), AnilaPgVectorRetriever)


def test_csp_wins_over_anila_pgvector(clean_env):
    # 兩者都可滿足（id + PGVECTOR_URL + CSP base）→ csp_http 先命中。
    clean_env.setenv("ANILA_CSP_BASE_URL", "https://csp")
    clean_env.setenv("ANILA_COLLECTION_ID", "2")
    clean_env.setenv("PGVECTOR_URL", "postgresql://u@h/db")
    clean_env.setenv("ANILA_API_KEY", "k")
    clean_env.setenv("ANILA_BASE_URL", "http://e/v1")
    assert isinstance(select_retriever(_cfg()), CspHttpRetriever)


def test_csp_misconfig_surfaces_as_pgvector_raise(clean_env):
    # 設了 collection_id 但忘了 CSP base 也忘了 PGVECTOR_URL：
    # csp_http 回 None → anila_pgvector 因缺 PGVECTOR_URL 而 RAISE（錯誤來源是 pgvector）。
    clean_env.setenv("ANILA_COLLECTION_ID", "2")
    clean_env.setenv("ANILA_API_KEY", "k")
    with pytest.raises(ValueError, match="PGVECTOR_URL"):
        select_retriever(_cfg())


def test_generic_pgvector_partial_config_raises(clean_env):
    # PGVECTOR_URL 有、PGVECTOR_COLLECTION 無、無 ANILA_COLLECTION_ID
    # → 跳過 csp/anila，generic pgvector.from_env 因缺 collection 而 RAISE。
    clean_env.setenv("PGVECTOR_URL", "postgresql://u@h/db")
    with pytest.raises(ValueError, match="PGVECTOR_COLLECTION"):
        select_retriever(_cfg())
