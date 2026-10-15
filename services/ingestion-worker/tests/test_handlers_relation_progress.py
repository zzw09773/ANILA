"""Relation extraction is best-effort; failures must still reach the job row.

The three extractors (rule / LLM / similarity) used to swallow errors into
the worker log only. The document row said succeeded, and
latest_job_progress_message looked like a clean index. This pins the
failure count onto the job's progress_message so the governance UI can
show it.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from anila_core.ingestion.chunking_plugins.base import ChunkResult
from ingestion_worker import handlers


class _FakeConn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    async def execute(self, sql: str, *args):
        self.calls.append((sql, args))
        return "UPDATE 1"

    async def fetchrow(self, sql: str, *args):
        self.calls.append((sql, args))
        return None


class _FakePool:
    def __init__(self) -> None:
        self.conn = _FakeConn()

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


class _OneLeafChunker:
    requires_embedder = False

    def chunk(self, text, parse_meta, params):
        return [
            ChunkResult(
                content="hello",
                chunk_key="leaf-1",
                token_count=1,
                metadata={"chunk_type": "leaf"},
            )
        ]


class _FakeStore:
    def __init__(self, pool, collection_id):  # noqa: ARG002
        pass

    async def add_parent_chunks(self, **kwargs):  # noqa: ARG002
        return {}

    async def index_chunks(self, **kwargs):  # noqa: ARG002
        return None


class _Embedder:
    model_name = "nvidia/nv-embed-v2"
    native_dim = 4096

    async def embed(self, texts, user_id=None):  # noqa: ARG002
        return [[0.1] * 8 for _ in texts]

    async def close(self):
        return None


def _progress_messages(pool: _FakePool) -> list[str]:
    out: list[str] = []
    for sql, args in pool.conn.calls:
        if "progress_message" not in sql:
            continue
        out.extend(
            a for a in args if isinstance(a, str) and a not in {"running", "succeeded"}
        )
    return out


@pytest.fixture
def wired_success(monkeypatch, tmp_path):
    blob = tmp_path / "note.txt"
    blob.write_text("hello", encoding="utf-8")

    async def fake_load_meta(pool, document_id):  # noqa: ARG001
        return {
            "collection_id": 7,
            "storage_path": str(blob),
            "filename": "note.txt",
            "mime_type": "text/plain",
            "chunking_config": {"strategy": "hierarchical"},
            "uploaded_by": 3,
            "owner_user_id": 3,
        }

    def fake_extract(filename, blob_bytes, mime_type):  # noqa: ARG001
        return ("hello", {"title": "note"}, {})

    async def fake_resolve(
        pool, *, settings_fallback_name, settings_fallback_native=None  # noqa: ARG001
    ):
        return SimpleNamespace(name="nvidia/nv-embed-v2", native_dim=4096)

    import ingestion_worker.platform_embedding as pe

    monkeypatch.setattr(handlers, "_load_document_meta", fake_load_meta)
    monkeypatch.setattr(handlers, "extract_text", fake_extract)
    monkeypatch.setattr(handlers, "get_chunker", lambda strategy: _OneLeafChunker())
    monkeypatch.setattr(pe, "resolve_from_pool", fake_resolve)
    monkeypatch.setattr(handlers, "CollectionScopedPgVectorStore", _FakeStore)

    async def parser_refresh(*args):
        return None

    async def captions(*args):
        return False, None

    async def no_relations(*args, **kwargs):
        return {"extracted": 0, "edges": 0}

    monkeypatch.setattr(handlers, "refresh_document_parser", parser_refresh)
    monkeypatch.setattr(handlers, "_resolve_caption_intent", captions)
    monkeypatch.setattr("ingestion_worker.relations.extract_and_resolve", no_relations)
    monkeypatch.setattr("ingestion_worker.llm_relations.extract_and_resolve_llm", no_relations)
    monkeypatch.setattr("ingestion_worker.similarity_relations.recompute_similarity_edges", no_relations)

    pool = _FakePool()
    ctx = {
        "pool": pool,
        "embedder": _Embedder(),
        "job_id": "arq-job-rel-fail",
    }
    return ctx, pool


async def test_relation_extractor_failures_are_folded_into_job_message(
    wired_success, monkeypatch
):
    """Mutant: keep the log-only excepts — the job message stays a clean index."""
    ctx, pool = wired_success

    async def boom(*args, **kwargs):  # noqa: ARG001
        raise RuntimeError("extractor down")

    import ingestion_worker.relations as rel_mod
    import ingestion_worker.llm_relations as llm_mod
    import ingestion_worker.similarity_relations as sim_mod

    monkeypatch.setattr(rel_mod, "extract_and_resolve", boom)
    monkeypatch.setattr(llm_mod, "extract_and_resolve_llm", boom)
    monkeypatch.setattr(sim_mod, "recompute_similarity_edges", boom)

    result = await handlers.ingest_document(ctx, 41)
    assert result["chunk_count"] == 1

    messages = _progress_messages(pool)
    assert any("3 條關聯抽取失敗" in m for m in messages), messages
    assert any("1 leaves + 0 parents indexed" in m for m in messages), messages


async def test_parser_fallback_quality_is_visible_in_job_message(wired_success, monkeypatch):
    ctx, pool = wired_success

    async def refresh(*args):
        return None

    monkeypatch.setattr(handlers, "refresh_document_parser", refresh)
    monkeypatch.setattr(
        handlers, "extract_text", lambda *args: (
            "hello", {"actual_parser": "DocxParser", "parser_fallback": True,
                      "quality_notice": "已使用院內離線解析；複雜版面請核對原檔。"}, {},
        ),
    )
    result = await handlers.ingest_document(ctx, 42)
    assert result["actual_parser"] == "DocxParser"
    assert result["parser_fallback"] is True
    assert any("解析器：DocxParser" in message and "核對原檔" in message
               for message in _progress_messages(pool))


async def test_one_failed_file_does_not_prevent_the_next_job(wired_success, monkeypatch):
    from anila_core.ingestion.errors import ParseError
    ctx, pool = wired_success
    calls = 0

    async def refresh(*args):
        return None

    def extract(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ParseError.corrupt("此檔案已損毀。", details={"reason": "corrupt_fixture"})
        return "hello", {"actual_parser": "PlainTextParser"}, {}

    monkeypatch.setattr(handlers, "refresh_document_parser", refresh)
    monkeypatch.setattr(handlers, "extract_text", extract)
    with pytest.raises(ParseError):
        await handlers.ingest_document(ctx, 43)
    result = await handlers.ingest_document(ctx, 44)
    assert result["chunk_count"] == 1
    assert result["actual_parser"] == "PlainTextParser"
