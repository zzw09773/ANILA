"""AnilaPgVectorRetriever tests.

Pure-Python tests against the helpers and `from_env()` configuration logic.
The full search path requires a live Postgres + embedding endpoint and is
covered by the manual smoke test (`scripts/smoke_anila_pgvector.py` if added);
here we keep the matrix runnable on CI without infrastructure.
"""

from __future__ import annotations

import pytest

from anila_agent.retrieval.anila_pgvector import (
    AnilaPgVectorRetriever,
    _format_halfvec,
    _normalize_dsn,
    _parse_metadata,
    from_env,
)

# ---------------------------------------------------------------------------
# Helper: env isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "PGVECTOR_URL",
        "ANILA_COLLECTION_ID",
        "ANILA_EMBED_MODEL",
        "ANILA_EMBED_BASE_URL",
        "ANILA_EMBED_API_KEY",
        "ANILA_BASE_URL",
        "ANILA_API_KEY",
        "ANILA_SSL_VERIFY",
    ):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# DSN normalisation
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw,expected",
    [
        # SQLAlchemy psycopg2 prefix → strip
        (
            "postgresql+psycopg2://u:p@h:5432/db",
            "postgresql://u:p@h:5432/db",
        ),
        # SQLAlchemy psycopg3 prefix → strip
        (
            "postgresql+psycopg://u:p@h/db",
            "postgresql://u:p@h/db",
        ),
        # Bare DSN → unchanged
        (
            "postgresql://csp:csp@127.0.0.1:5433/csp",
            "postgresql://csp:csp@127.0.0.1:5433/csp",
        ),
    ],
)
def test_normalize_dsn(raw: str, expected: str) -> None:
    assert _normalize_dsn(raw) == expected


# ---------------------------------------------------------------------------
# halfvec text formatting
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_format_halfvec_basic() -> None:
    out = _format_halfvec([1.0, -0.5, 0.0])
    assert out == "[1,-0.5,0]"


@pytest.mark.unit
def test_format_halfvec_uses_compact_g_format() -> None:
    """Long floats are trimmed but precision is preserved enough for cosine."""
    out = _format_halfvec([0.123456789, 1e-7])
    # ".6g" gives up to 6 significant digits.
    assert out == "[0.123457,1e-07]"


# ---------------------------------------------------------------------------
# JSONB metadata parsing
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, {}),
        ({}, {}),
        ({"k": "v"}, {"k": "v"}),
        ('{"k": "v"}', {"k": "v"}),
        (b'{"k": "v"}', {"k": "v"}),
        ("not json", {}),
        ("[1,2,3]", {}),  # JSON array → not a dict → empty
        ("null", {}),
    ],
)
def test_parse_metadata(raw: object, expected: dict) -> None:
    assert _parse_metadata(raw) == expected


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_constructor_rejects_non_int_collection_id() -> None:
    with pytest.raises(ValueError, match="collection_id"):
        AnilaPgVectorRetriever(
            url="postgresql://x@h/x",
            collection_id="52",  # type: ignore[arg-type]
            embed_base_url="http://e/v1",
            embed_api_key="k",
            embed_model="m",
        )


@pytest.mark.unit
def test_constructor_rejects_bool_collection_id() -> None:
    """`bool` is a subclass of int — guard against `True` silently scoping to id=1."""
    with pytest.raises(ValueError, match="collection_id"):
        AnilaPgVectorRetriever(
            url="postgresql://x@h/x",
            collection_id=True,  # type: ignore[arg-type]
            embed_base_url="http://e/v1",
            embed_api_key="k",
            embed_model="m",
        )


@pytest.mark.unit
def test_constructor_rejects_non_positive_collection_id() -> None:
    with pytest.raises(ValueError, match="> 0"):
        AnilaPgVectorRetriever(
            url="postgresql://x@h/x",
            collection_id=0,
            embed_base_url="http://e/v1",
            embed_api_key="k",
            embed_model="m",
        )


@pytest.mark.unit
def test_constructor_normalizes_sqlalchemy_dsn() -> None:
    r = AnilaPgVectorRetriever(
        url="postgresql+psycopg2://u:p@h/db",
        collection_id=1,
        embed_base_url="http://e/v1/",  # trailing slash
        embed_api_key="k",
        embed_model="m",
    )
    assert r._dsn == "postgresql://u:p@h/db"
    assert r._embed_base_url == "http://e/v1"  # trailing slash stripped


@pytest.mark.unit
def test_metadata_property_exposes_backend_and_collection() -> None:
    r = AnilaPgVectorRetriever(
        url="postgresql://x@h/x",
        collection_id=42,
        embed_base_url="http://e/v1",
        embed_api_key="k",
        embed_model="my-embed",
    )
    assert r.name == "anila-pgvector:collection=42"
    assert r.metadata == {
        "backend": "anila-pgvector",
        "collection_id": 42,
        "embed_model": "my-embed",
    }


# ---------------------------------------------------------------------------
# from_env() configuration handling
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_from_env_unset_returns_none() -> None:
    """Unconfigured → opt out so caller can try the langchain flavour."""
    assert from_env() is None


@pytest.mark.unit
def test_from_env_collection_only_url_missing_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANILA_COLLECTION_ID", "52")
    with pytest.raises(ValueError, match="PGVECTOR_URL is missing"):
        from_env()


@pytest.mark.unit
def test_from_env_url_only_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """PGVECTOR_URL alone is the langchain trigger; we opt out here."""
    monkeypatch.setenv("PGVECTOR_URL", "postgresql://x@h/x")
    assert from_env() is None


@pytest.mark.unit
def test_from_env_non_int_collection_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGVECTOR_URL", "postgresql://x@h/x")
    monkeypatch.setenv("ANILA_COLLECTION_ID", "abc")
    with pytest.raises(ValueError, match="must be an int"):
        from_env()


@pytest.mark.unit
def test_from_env_missing_embed_base_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGVECTOR_URL", "postgresql://x@h/x")
    monkeypatch.setenv("ANILA_COLLECTION_ID", "1")
    monkeypatch.setenv("ANILA_API_KEY", "k")  # only key, no base_url
    with pytest.raises(ValueError, match="ANILA_EMBED_BASE_URL"):
        from_env()


@pytest.mark.unit
def test_from_env_missing_embed_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGVECTOR_URL", "postgresql://x@h/x")
    monkeypatch.setenv("ANILA_COLLECTION_ID", "1")
    monkeypatch.setenv("ANILA_BASE_URL", "http://chat/v1")
    with pytest.raises(ValueError, match="ANILA_EMBED_API_KEY"):
        from_env()


@pytest.mark.unit
def test_from_env_falls_back_to_chat_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PGVECTOR_URL", "postgresql://x@h/x")
    monkeypatch.setenv("ANILA_COLLECTION_ID", "52")
    monkeypatch.setenv("ANILA_BASE_URL", "https://chat/v1")
    monkeypatch.setenv("ANILA_API_KEY", "sk-shared")

    r = from_env()
    assert r is not None
    assert r._embed_base_url == "https://chat/v1"
    assert r._embed_api_key == "sk-shared"
    assert r._embed_model == "nvidia/NV-embed-V2"  # default
    assert r._verify_ssl is True  # default


@pytest.mark.unit
def test_from_env_dedicated_embed_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGVECTOR_URL", "postgresql://x@h/x")
    monkeypatch.setenv("ANILA_COLLECTION_ID", "52")
    monkeypatch.setenv("ANILA_BASE_URL", "https://chat/v1")
    monkeypatch.setenv("ANILA_API_KEY", "chat-key")
    monkeypatch.setenv("ANILA_EMBED_BASE_URL", "https://embed/v1")
    monkeypatch.setenv("ANILA_EMBED_API_KEY", "embed-key")
    monkeypatch.setenv("ANILA_EMBED_MODEL", "custom/model")

    r = from_env()
    assert r is not None
    assert r._embed_base_url == "https://embed/v1"
    assert r._embed_api_key == "embed-key"
    assert r._embed_model == "custom/model"


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw,expected_verify",
    [("0", False), ("false", False), ("False", False), ("no", False), ("off", False),
     ("1", True), ("true", True), ("yes", True), ("", True)],
)
def test_from_env_ssl_verify_flag(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected_verify: bool
) -> None:
    monkeypatch.setenv("PGVECTOR_URL", "postgresql://x@h/x")
    monkeypatch.setenv("ANILA_COLLECTION_ID", "1")
    monkeypatch.setenv("ANILA_BASE_URL", "https://chat/v1")
    monkeypatch.setenv("ANILA_API_KEY", "k")
    monkeypatch.setenv("ANILA_SSL_VERIFY", raw)

    r = from_env()
    assert r is not None
    assert r._verify_ssl is expected_verify


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_retriever_satisfies_protocol() -> None:
    from anila_agent.retrieval.base import Retriever

    r = AnilaPgVectorRetriever(
        url="postgresql://x@h/x",
        collection_id=1,
        embed_base_url="http://e/v1",
        embed_api_key="k",
        embed_model="m",
    )
    assert isinstance(r, Retriever)
