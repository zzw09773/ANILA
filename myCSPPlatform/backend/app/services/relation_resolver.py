"""Resolution + reconciliation for cross-document relations (design v2 §6).

The *tested reference* for turning extracted citations into resolved
``document_relations`` edges. Order-independent and idempotent:

  * :func:`replace_rule_edges` — delete-then-insert this document's
    ``source='rule'`` edges, leaving ``source='manual'`` untouched (§6 step 1):
    re-extract stays clean, never duplicates, never clobbers human edges.
  * :func:`resolve_pending` — match every dst-NULL rule/manual edge's target
    name against the collection's document titles and back-fill
    ``dst_document_id``. Re-running changes nothing (idempotent); running it
    after a late target finally appears resolves the edge (order-independent —
    covers both "補充先到、母法後到" and the reverse).
  * :func:`create_manual_edge` / :func:`delete_manual_edge` — human edges.

The bug-prone matching/ambiguity decision is delegated to
``anila_core.ingestion.relation_resolution.match_target`` so the worker
(asyncpg) and this service share ONE policy. The worker deposits unresolved
rule edges via asyncpg at ingest (it owns the parsed text); this service
resolves them — lazily on read and on explicit reresolve — so the matching
path is exercised by the CSP SQLite test suite, which the worker's DB layer
can't be stood up for deterministically.

All functions ``flush`` but never ``commit`` — the caller owns the
transaction boundary (one transaction per ingest / request, per §6).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

from anila_core.ingestion.citation_extractor import Citation, normalize_title
from anila_core.ingestion.relation_resolution import match_target, ref_title

from app.models.ingestion import DocumentRelation, IngestionDocument


def scope_collection_rls(db: Session, collection_id: int) -> None:
    """Scope the session's current transaction to ``collection_id`` for RLS.

    ``document_relations`` is FORCE-RLS (migration 0039) and the CSP backend
    connects as the non-superuser ``csp_app`` role in dev/prod, so without this
    the policy (``collection_id = NULLIF(current_setting(...), '')::int``)
    silently returns ZERO rows — reads come back empty and writes fail the
    WITH CHECK. ``set_config(..., is_local => true)`` is SET LOCAL: bound to the
    current transaction, never leaking to the next pooled checkout.

    No-op on SQLite (the pytest fixture has no RLS and no ``set_config``).
    """
    bind = db.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        db.execute(
            text("SELECT set_config('anila.collection_id', :cid, true)"),
            {"cid": str(int(collection_id))},
        )


@dataclass(frozen=True)
class ResolveCounts:
    """How many dst-NULL edges moved where during a :func:`resolve_pending`."""

    resolved: int = 0
    still_unresolved: int = 0
    ambiguous: int = 0


@dataclass(frozen=True)
class ReresolveResult(ResolveCounts):
    """A full collection re-extract + reconcile pass."""

    rule_edges_extracted: int = 0


def _collection_candidates(db: Session, collection_id: int) -> list[tuple[int, str]]:
    """``[(document_id, normalized_title)]`` for every titled doc in the collection."""
    rows = (
        db.query(IngestionDocument.id, IngestionDocument.normalized_title)
        .filter(
            IngestionDocument.collection_id == collection_id,
            IngestionDocument.normalized_title.isnot(None),
            IngestionDocument.normalized_title != "",
        )
        .all()
    )
    return [(row[0], row[1]) for row in rows]


def replace_rule_edges(
    db: Session,
    *,
    collection_id: int,
    src_document_id: int,
    citations: Iterable[Citation],
    run_id: str | None,
    flush: bool = True,
) -> int:
    """Delete this document's ``rule`` edges then insert the fresh batch.

    ``manual`` edges are never touched. Edges are inserted unresolved
    (``dst_document_id`` NULL); :func:`resolve_pending` fills them. Returns the
    number of rule edges inserted.
    """
    scope_collection_rls(db, collection_id)
    db.query(DocumentRelation).filter(
        DocumentRelation.collection_id == collection_id,
        DocumentRelation.src_document_id == src_document_id,
        DocumentRelation.source == "rule",
    ).delete(synchronize_session=False)

    inserted = 0
    seen: set[tuple[str, str]] = set()
    for c in citations:
        # UNIQUE(collection_id, src, target_ref, relation_type, source) — the
        # extractor already dedups, but guard against a duplicate batch entry.
        key = (c.target_ref, c.relation_type)
        if key in seen:
            continue
        seen.add(key)
        db.add(
            DocumentRelation(
                collection_id=collection_id,
                src_document_id=src_document_id,
                dst_document_id=None,
                target_ref=c.target_ref,
                relation_type=c.relation_type,
                confidence=1.0,
                source="rule",
                extractor_run_id=run_id,
                evidence=c.evidence,
            )
        )
        inserted += 1

    if flush:
        db.flush()
    return inserted


def resolve_pending(db: Session, *, collection_id: int) -> ResolveCounts:
    """Back-fill ``dst_document_id`` for every dst-NULL rule/manual edge.

    Idempotent + order-independent: matches each edge's target name against the
    collection's current document titles. Ambiguous (>1 match) and unresolved
    (0 matches) edges are left NULL — we never silently pick one (§6 step 4).
    """
    scope_collection_rls(db, collection_id)
    candidates = _collection_candidates(db, collection_id)
    pending = (
        db.query(DocumentRelation)
        .filter(
            DocumentRelation.collection_id == collection_id,
            DocumentRelation.dst_document_id.is_(None),
            DocumentRelation.source.in_(("rule", "manual")),
        )
        .all()
    )
    resolved = ambiguous = still = 0
    for edge in pending:
        res = match_target(
            ref_title(edge.target_ref),
            candidates,
            exclude_doc_id=edge.src_document_id,
        )
        if res.resolved:
            edge.dst_document_id = res.document_id
            resolved += 1
        elif res.ambiguous:
            ambiguous += 1
        else:
            still += 1
    db.flush()
    return ResolveCounts(resolved=resolved, still_unresolved=still, ambiguous=ambiguous)


def apply_document_extraction(
    db: Session,
    *,
    collection_id: int,
    src_document_id: int,
    citations: Iterable[Citation],
    run_id: str | None,
) -> ResolveCounts:
    """Ingest-time path (the reference the worker mirrors in asyncpg): replace
    this document's rule edges, then reconcile the whole collection so the new
    document's out-going edges resolve AND any pre-existing in-going edges that
    were waiting for it get back-filled — all in the caller's transaction."""
    replace_rule_edges(
        db,
        collection_id=collection_id,
        src_document_id=src_document_id,
        citations=citations,
        run_id=run_id,
    )
    return resolve_pending(db, collection_id=collection_id)


def reresolve_collection(
    db: Session,
    *,
    collection_id: int,
    citations_by_doc: Mapping[int, Sequence[Citation]],
    run_id: str | None,
) -> ReresolveResult:
    """Re-extract every document then reconcile once (the ``:reresolve`` path).

    ``citations_by_doc`` is ``{document_id: [Citation, ...]}`` — the caller
    (the worker, which has each blob's parsed text) supplies it.
    """
    extracted = 0
    for doc_id, cites in citations_by_doc.items():
        extracted += replace_rule_edges(
            db,
            collection_id=collection_id,
            src_document_id=doc_id,
            citations=cites,
            run_id=run_id,
            flush=False,
        )
    db.flush()
    counts = resolve_pending(db, collection_id=collection_id)
    return ReresolveResult(
        resolved=counts.resolved,
        still_unresolved=counts.still_unresolved,
        ambiguous=counts.ambiguous,
        rule_edges_extracted=extracted,
    )


def create_manual_edge(
    db: Session,
    *,
    collection_id: int,
    src_document_id: int,
    relation_type: str,
    dst_document_id: int | None = None,
    target_ref: str | None = None,
    evidence: str | None = None,
    created_by_user_id: int | None = None,
) -> DocumentRelation:
    """Insert a human ``source='manual'`` edge.

    The target is either a concrete ``dst_document_id`` (resolved immediately,
    ``target_ref`` derived from its title) or an unresolved ``target_ref`` name
    (normalized so the no-whitespace-in-name invariant holds; resolved if a
    match already exists). Raises ``ValueError`` on a bad target.
    """
    scope_collection_rls(db, collection_id)
    if dst_document_id is not None:
        dst = (
            db.query(IngestionDocument)
            .filter(
                IngestionDocument.id == dst_document_id,
                IngestionDocument.collection_id == collection_id,
            )
            .first()
        )
        if dst is None:
            raise ValueError("dst_document_id is not a document in this collection")
        stored_ref = (
            normalize_title(target_ref) if target_ref else None
        ) or dst.normalized_title or normalize_title(dst.title or "") or f"doc:{dst_document_id}"
    else:
        stored_ref = normalize_title(target_ref or "")
        if not stored_ref:
            raise ValueError("one of dst_document_id or target_ref is required")

    edge = DocumentRelation(
        collection_id=collection_id,
        src_document_id=src_document_id,
        dst_document_id=dst_document_id,
        target_ref=stored_ref,
        relation_type=relation_type,
        confidence=1.0,
        source="manual",
        evidence=evidence,
        created_by_user_id=created_by_user_id,
    )
    db.add(edge)
    db.flush()
    if dst_document_id is None:
        resolve_pending(db, collection_id=collection_id)
        db.refresh(edge)
    return edge


def delete_manual_edge(db: Session, *, rel_id: int) -> bool:
    """Delete a ``manual`` edge. Rule edges are managed by re-extract, never the
    DELETE endpoint — returns False (no-op) for a non-existent or non-manual id."""
    edge = (
        db.query(DocumentRelation)
        .filter(DocumentRelation.id == rel_id, DocumentRelation.source == "manual")
        .first()
    )
    if edge is None:
        return False
    db.delete(edge)
    db.flush()
    return True


def annotate_ambiguity(
    db: Session, *, collection_id: int, edges: Sequence[DocumentRelation]
) -> dict[int, bool]:
    """``{edge_id: ambiguous}`` for unresolved edges — the API flags these in
    the relations tab. Candidates are fetched once (O(edges × docs), fine at
    Phase-1 collection sizes)."""
    scope_collection_rls(db, collection_id)
    candidates = _collection_candidates(db, collection_id)
    out: dict[int, bool] = {}
    for edge in edges:
        if edge.dst_document_id is not None:
            out[edge.id] = False
            continue
        res = match_target(
            ref_title(edge.target_ref),
            candidates,
            exclude_doc_id=edge.src_document_id,
        )
        out[edge.id] = res.ambiguous
    return out
