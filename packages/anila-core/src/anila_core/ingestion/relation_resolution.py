"""Pure target-resolution policy for cross-document relations (design v2 §6).

Given a cited regulation NAME and the candidate documents in a collection,
decide which document a citation resolves to — or that it is *unresolved* /
*ambiguous*. There is **no DB here**: the ingestion-worker (asyncpg) and the
CSP service (SQLAlchemy) each fetch the candidate list their own way and feed
it in, so this — the genuinely bug-prone bit — is written and tested exactly
once instead of once per DB driver.

Matching policy (Phase 1):
  1. **exact** ``normalized_title == target_name`` — the reliable path.
  2. **contains** (only if no exact) — the cited short name is a substring of a
     longer official title, or vice-versa. A convenience, not authoritative.
  3. **>1 match at any tier → ambiguous** (left unresolved; we never silently
     pick one — design §6 step 4).

``target_name`` is assumed already NFKC/bracket-normalized (callers pass
``Citation.target_title`` or :func:`ref_title`). A document never resolves to
itself (``exclude_doc_id``).
"""
from __future__ import annotations

from dataclasses import dataclass

__all__ = ["MatchResult", "match_target", "ref_title"]


@dataclass(frozen=True)
class MatchResult:
    """Outcome of resolving one cited name against a collection's documents."""

    document_id: int | None  # the resolved target, or None
    ambiguous: bool  # True iff >1 candidate matched (kept unresolved)
    candidate_ids: tuple[int, ...]  # every matched id (evidence / logging)

    @property
    def resolved(self) -> bool:
        return self.document_id is not None


def ref_title(target_ref: str | None) -> str:
    """Return the regulation-name part of a stored ``target_ref``.

    Invariant enforced at every write site: the name part is
    ``normalize_title``-d (so it contains **no whitespace**) and an optional
    article is appended after a single space — ``"員工差勤管理辦法 第3條"``.
    The name is therefore everything before the first space.
    """
    if not target_ref:
        return ""
    return target_ref.split(" ", 1)[0]


def match_target(
    target_name: str,
    candidates: list[tuple[int, str]],
    *,
    exclude_doc_id: int | None = None,
) -> MatchResult:
    """Resolve ``target_name`` against ``candidates`` = ``[(doc_id, normalized_title)]``.

    Exact match wins; contains is the fallback; >1 at either tier is ambiguous.
    """
    if not target_name:
        return MatchResult(None, False, ())

    def _decide(ids: list[int]) -> MatchResult | None:
        if len(ids) == 1:
            return MatchResult(ids[0], False, (ids[0],))
        if len(ids) > 1:
            return MatchResult(None, True, tuple(ids))
        return None  # 0 → caller falls through to the next tier

    exact = [
        doc_id
        for doc_id, nt in candidates
        if doc_id != exclude_doc_id and nt == target_name
    ]
    decided = _decide(exact)
    if decided is not None:
        return decided

    contains = [
        doc_id
        for doc_id, nt in candidates
        if doc_id != exclude_doc_id
        and nt
        and (target_name in nt or nt in target_name)
    ]
    decided = _decide(contains)
    if decided is not None:
        return decided

    return MatchResult(None, False, ())
