"""Worker-side citation extraction + edge deposit (design v2 §5/§6).

Runs inside ``ingest_document`` once a document is indexed — this is the only
place the parsed text exists. Extraction uses the shared
``anila_core.ingestion.citation_extractor``; the resolve / ambiguity decision
uses the shared ``relation_resolution.match_target`` — so this asyncpg path and
the CSP SQLAlchemy service (``app.services.relation_resolver``, the *tested
reference*) apply ONE identical policy. Only the SQL plumbing differs per
driver; the bug-prone matching lives in anila_core and is unit-tested there.

``document_relations`` is RLS-guarded on the ``anila.collection_id`` GUC
(migration 0039) and the worker connects as the non-superuser ``csp_app`` role,
so every statement runs inside a transaction that first ``SET LOCAL
anila.collection_id`` — exactly like ``CollectionScopedPgVectorStore``.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Sequence

from anila_core.ingestion.citation_extractor import Citation, extract_citations
from anila_core.ingestion.relation_resolution import match_target, ref_title

logger = logging.getLogger(__name__)


async def _candidates(conn: Any, collection_id: int) -> list[tuple[int, str]]:
    rows = await conn.fetch(
        "SELECT id, normalized_title FROM ingestion_documents "
        "WHERE collection_id = $1 AND normalized_title IS NOT NULL "
        "AND normalized_title <> ''",
        collection_id,
    )
    return [(r["id"], r["normalized_title"]) for r in rows]


async def _deposit_rule_edges(
    conn: Any,
    collection_id: int,
    document_id: int,
    citations: Iterable[Citation],
    run_id: str | None,
) -> int:
    """Delete this doc's ``rule`` edges (manual untouched) then insert the fresh
    batch unresolved. Returns the number actually inserted."""
    await conn.execute(
        "DELETE FROM document_relations "
        "WHERE collection_id = $1 AND src_document_id = $2 AND source = 'rule'",
        collection_id,
        document_id,
    )
    seen: set[tuple[str, str]] = set()
    inserted = 0
    for c in citations:
        key = (c.target_ref, c.relation_type)
        if key in seen:
            continue
        seen.add(key)
        status = await conn.execute(
            "INSERT INTO document_relations "
            "(collection_id, src_document_id, dst_document_id, target_ref, "
            " relation_type, confidence, source, extractor_run_id, evidence) "
            "VALUES ($1, $2, NULL, $3, $4, 1.0, 'rule', $5, $6) "
            "ON CONFLICT (collection_id, src_document_id, target_ref, relation_type, source) "
            "DO NOTHING",
            collection_id,
            document_id,
            c.target_ref,
            c.relation_type,
            run_id,
            c.evidence,
        )
        # command tag is "INSERT 0 1" on insert, "INSERT 0 0" on conflict-skip
        if isinstance(status, str) and status.rsplit(" ", 1)[-1] == "1":
            inserted += 1
    return inserted


async def _resolve_pending(conn: Any, collection_id: int) -> int:
    """Back-fill dst for every dst-NULL rule/manual edge — idempotent +
    order-independent, mirroring ``relation_resolver.resolve_pending``."""
    candidates = await _candidates(conn, collection_id)
    pending = await conn.fetch(
        "SELECT id, src_document_id, target_ref FROM document_relations "
        "WHERE collection_id = $1 AND dst_document_id IS NULL "
        "AND source IN ('rule', 'manual')",
        collection_id,
    )
    resolved = 0
    for edge in pending:
        res = match_target(
            ref_title(edge["target_ref"]),
            candidates,
            exclude_doc_id=edge["src_document_id"],
        )
        if res.resolved:
            await conn.execute(
                "UPDATE document_relations SET dst_document_id = $1 WHERE id = $2",
                res.document_id,
                edge["id"],
            )
            resolved += 1
    return resolved


async def extract_and_resolve(
    pool: Any,
    *,
    collection_id: int,
    document_id: int,
    text: str,
    run_id: str | None,
) -> dict[str, int]:
    """Ingest-time path: extract citations from ``text``, deposit this doc's
    rule edges, then reconcile the whole collection (out-going for this doc +
    in-going back-fill for edges that were waiting on it). One transaction;
    RLS GUC scoped to ``collection_id``."""
    citations = extract_citations(text or "")
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                f"SET LOCAL anila.collection_id = {int(collection_id)}"
            )
            extracted = await _deposit_rule_edges(
                conn, collection_id, document_id, citations, run_id
            )
            resolved = await _resolve_pending(conn, collection_id)
    return {"extracted": extracted, "resolved": resolved}


async def reresolve_collection_edges(
    pool: Any,
    *,
    collection_id: int,
    docs: Sequence[tuple[int, str]],
    run_id: str | None,
) -> dict[str, int]:
    """Re-extract every document then reconcile once (the ``:reresolve`` job
    body). ``docs`` is ``[(document_id, parsed_text), ...]`` — the caller
    re-parses each blob."""
    per_doc = [(doc_id, extract_citations(text or "")) for doc_id, text in docs]
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                f"SET LOCAL anila.collection_id = {int(collection_id)}"
            )
            extracted = 0
            for doc_id, citations in per_doc:
                extracted += await _deposit_rule_edges(
                    conn, collection_id, doc_id, citations, run_id
                )
            resolved = await _resolve_pending(conn, collection_id)
    return {"extracted": extracted, "resolved": resolved}
