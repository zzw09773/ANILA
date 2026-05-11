"""PgVectorRetriever tests.

The real langchain-postgres + langchain-openai libraries are NOT required to
run these tests — `monkeypatch.setitem(sys.modules, ...)` injects fakes that
the lazy imports inside `PgVectorRetriever.__init__` pick up. This keeps the
test matrix runnable on CI machines that don't have the optional `[pgvector]`
extra installed and don't have a Postgres instance to talk to.
"""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _install_fake_langchain(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any]:
    """Inject minimal langchain_postgres + langchain_openai stand-ins.

    Returns the two fake module objects so individual tests can assert on
    constructor calls and configure return values for `PGVector(...)`.
    """
    fake_pg = MagicMock(name="langchain_postgres_fake")
    fake_pg.PGVector = MagicMock(name="PGVector")
    fake_oa = MagicMock(name="langchain_openai_fake")
    fake_oa.OpenAIEmbeddings = MagicMock(name="OpenAIEmbeddings")
    monkeypatch.setitem(sys.modules, "langchain_postgres", fake_pg)
    monkeypatch.setitem(sys.modules, "langchain_openai", fake_oa)
    return fake_pg, fake_oa


class _FakeLangchainDoc:
    """Stand-in for langchain_core.documents.Document — only the fields we read."""

    def __init__(
        self,
        page_content: str,
        metadata: dict[str, Any] | None = None,
        id: str | None = None,
    ) -> None:
        self.page_content = page_content
        self.metadata = metadata or {}
        self.id = id


@pytest.fixture(autouse=True)
def _isolate_pgvector_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wipe all PGVECTOR_* / ANILA_EMBED_* vars so each test starts clean."""
    for var in (
        "PGVECTOR_URL",
        "PGVECTOR_COLLECTION",
        "ANILA_EMBED_MODEL",
        "ANILA_EMBED_BASE_URL",
        "ANILA_EMBED_API_KEY",
        "ANILA_BASE_URL",
        "ANILA_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# from_env() configuration handling
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_from_env_returns_none_when_unset() -> None:
    """Unconfigured env → None so the caller stays on DummyRetriever."""
    from anila_agent.retrieval.pgvector import from_env

    assert from_env() is None


@pytest.mark.unit
def test_from_env_url_without_collection_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Half-configured deployment must fail loud, not silently fall back."""
    monkeypatch.setenv("PGVECTOR_URL", "postgresql+psycopg2://x@localhost/x")
    from anila_agent.retrieval.pgvector import from_env

    with pytest.raises(ValueError, match="PGVECTOR_COLLECTION"):
        from_env()


@pytest.mark.unit
def test_from_env_passes_url_and_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_pg, _fake_oa = _install_fake_langchain(monkeypatch)
    monkeypatch.setenv("PGVECTOR_URL", "postgresql+psycopg2://u:p@h:5432/db")
    monkeypatch.setenv("PGVECTOR_COLLECTION", "mydocs")

    from anila_agent.retrieval.pgvector import from_env

    retriever = from_env()
    assert retriever is not None
    assert retriever.name == "pgvector:mydocs"

    pgv_kwargs = fake_pg.PGVector.call_args.kwargs
    assert pgv_kwargs["collection_name"] == "mydocs"
    assert pgv_kwargs["connection"] == "postgresql+psycopg2://u:p@h:5432/db"
    assert pgv_kwargs["use_jsonb"] is True


@pytest.mark.unit
def test_from_env_default_embed_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """ANILA_EMBED_MODEL unset → text-embedding-3-small default."""
    _, fake_oa = _install_fake_langchain(monkeypatch)
    monkeypatch.setenv("PGVECTOR_URL", "postgresql://x@h/x")
    monkeypatch.setenv("PGVECTOR_COLLECTION", "c")

    from anila_agent.retrieval.pgvector import from_env

    from_env()
    assert (
        fake_oa.OpenAIEmbeddings.call_args.kwargs["model"] == "text-embedding-3-small"
    )


@pytest.mark.unit
def test_from_env_falls_back_to_anila_chat_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ANILA_EMBED_BASE_URL is unset, embeddings reuse the chat endpoint."""
    _, fake_oa = _install_fake_langchain(monkeypatch)
    monkeypatch.setenv("PGVECTOR_URL", "postgresql://x@h/x")
    monkeypatch.setenv("PGVECTOR_COLLECTION", "c")
    monkeypatch.setenv("ANILA_BASE_URL", "http://chat:8000/v1")
    monkeypatch.setenv("ANILA_API_KEY", "sk-shared")

    from anila_agent.retrieval.pgvector import from_env

    from_env()
    kwargs = fake_oa.OpenAIEmbeddings.call_args.kwargs
    assert kwargs["base_url"] == "http://chat:8000/v1"
    assert kwargs["api_key"] == "sk-shared"


@pytest.mark.unit
def test_embed_specific_env_overrides_chat_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dedicated ANILA_EMBED_* vars win over the chat endpoint fallback."""
    _, fake_oa = _install_fake_langchain(monkeypatch)
    monkeypatch.setenv("PGVECTOR_URL", "postgresql://x@h/x")
    monkeypatch.setenv("PGVECTOR_COLLECTION", "c")
    monkeypatch.setenv("ANILA_BASE_URL", "http://chat/v1")
    monkeypatch.setenv("ANILA_API_KEY", "chat-key")
    monkeypatch.setenv("ANILA_EMBED_BASE_URL", "http://embed/v1")
    monkeypatch.setenv("ANILA_EMBED_API_KEY", "embed-key")

    from anila_agent.retrieval.pgvector import from_env

    from_env()
    kwargs = fake_oa.OpenAIEmbeddings.call_args.kwargs
    assert kwargs["base_url"] == "http://embed/v1"
    assert kwargs["api_key"] == "embed-key"


# ---------------------------------------------------------------------------
# Lazy import / missing dependency handling
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_missing_libs_raises_with_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    """When langchain isn't installed, ImportError surfaces the install command."""
    monkeypatch.setitem(sys.modules, "langchain_postgres", None)
    monkeypatch.setitem(sys.modules, "langchain_openai", None)

    from anila_agent.retrieval.pgvector import PgVectorRetriever

    with pytest.raises(ImportError, match="langchain-postgres"):
        PgVectorRetriever(url="postgresql://x@h/x", collection="c")


# ---------------------------------------------------------------------------
# Adapter behaviour: langchain Document → anila Document
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_search_maps_documents_with_explicit_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_pg, _ = _install_fake_langchain(monkeypatch)
    fake_store = MagicMock()
    fake_store.asimilarity_search_with_score = AsyncMock(
        return_value=[
            (_FakeLangchainDoc("hello world", {"src": "test"}, id="doc-1"), 0.92),
        ]
    )
    fake_pg.PGVector.return_value = fake_store

    from anila_agent.retrieval.pgvector import PgVectorRetriever

    retriever = PgVectorRetriever(url="postgresql://x@h/x", collection="c")
    results = await retriever.search("query", k=3)

    fake_store.asimilarity_search_with_score.assert_called_once_with("query", k=3)
    assert len(results) == 1
    assert results[0].id == "doc-1"
    assert results[0].text == "hello world"
    assert results[0].score == 0.92
    assert results[0].metadata == {"src": "test"}


@pytest.mark.unit
async def test_search_id_fallback_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """id → metadata['id'] → metadata['chunk_id'] → enumerate index."""
    fake_pg, _ = _install_fake_langchain(monkeypatch)
    fake_store = MagicMock()
    fake_store.asimilarity_search_with_score = AsyncMock(
        return_value=[
            (_FakeLangchainDoc("a", {"id": "from-meta-id"}), 0.9),
            (_FakeLangchainDoc("b", {"chunk_id": "from-chunk-id"}), 0.8),
            (_FakeLangchainDoc("c", {}), 0.1),
        ]
    )
    fake_pg.PGVector.return_value = fake_store

    from anila_agent.retrieval.pgvector import PgVectorRetriever

    retriever = PgVectorRetriever(url="x", collection="c")
    results = await retriever.search("q")

    assert [r.id for r in results] == ["from-meta-id", "from-chunk-id", "2"]


@pytest.mark.unit
async def test_search_empty_results(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_pg, _ = _install_fake_langchain(monkeypatch)
    fake_store = MagicMock()
    fake_store.asimilarity_search_with_score = AsyncMock(return_value=[])
    fake_pg.PGVector.return_value = fake_store

    from anila_agent.retrieval.pgvector import PgVectorRetriever

    retriever = PgVectorRetriever(url="x", collection="c")
    assert await retriever.search("q") == []


@pytest.mark.unit
async def test_fetch_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Chunk-level retrieval doesn't need a separate full-document fetch."""
    _install_fake_langchain(monkeypatch)
    from anila_agent.retrieval.pgvector import PgVectorRetriever

    retriever = PgVectorRetriever(url="x", collection="c")
    assert await retriever.fetch("any-id") is None


@pytest.mark.unit
def test_retriever_satisfies_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    """PgVectorRetriever passes the runtime Retriever Protocol check."""
    _install_fake_langchain(monkeypatch)
    from anila_agent.retrieval.base import Retriever
    from anila_agent.retrieval.pgvector import PgVectorRetriever

    retriever = PgVectorRetriever(url="x", collection="c")
    assert isinstance(retriever, Retriever)
