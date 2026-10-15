"""Bounded cross-collection RAG through the existing caller-authorized API."""
from __future__ import annotations

from copy import deepcopy
import json
import math
import re
import unicodedata
from typing import Literal

from fastapi import HTTPException

from anila_core.embeddings.keyword import cjk_search_units
from app.api.ingestion import search as search_api

RetrievalMode = Literal["hybrid", "fulltext"]
MAX_COLLECTIONS = 30
MAX_DOCUMENTS = 20
MAX_CHARACTERS = 100_000
MAX_CHUNKS = 500
MAX_CONTEXT_BYTES = 120 * 1024
MAX_CANDIDATE_BYTES = 2 * 1024 * 1024
MAX_CANDIDATES = MAX_COLLECTIONS * 50
_ARTICLE = re.compile(r"第\s*[0-9一二三四五六七八九十百千零〇壹貳參肆伍陸柒捌玖拾佰仟]+\s*[條項款]")
_IDENTIFIER = re.compile(r"[A-Za-z0-9]+(?:[-_./][A-Za-z0-9]+)+")


def _error(code: int, kind: str, message: str):
    return HTTPException(code, detail={"error_type": kind, "message": message})


def _size(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _literal_match(literal: str, text: str) -> bool:
    if _ARTICLE.fullmatch(literal):
        return re.sub(r"\s+", "", literal) in re.sub(r"\s+", "", text)
    # AB-0012 must not count as the complete identifier AB-001. Chinese
    # surrounding text is permitted; only adjacent ASCII letters/digits
    # extend such an identifier.
    before = r"(?<![a-z0-9])" if literal[:1].isascii() and literal[:1].isalnum() else ""
    after = r"(?![a-z0-9])" if literal[-1:].isascii() and literal[-1:].isalnum() else ""
    return bool(literal) and re.search(before + re.escape(literal) + after, text) is not None


def _lexical_key(query: str, hit: dict) -> tuple[float, float, float]:
    """Preserve exact article/identifier recall across collection-local ranks.

    API scores are collection-local RRF scores; they cannot make the first
    collection inherently better. Literal relevance reranks the union,
    then local hybrid scores break ties. No extra model or corpus read.
    """
    query = _normalize(query).strip()
    text = _normalize(hit["content"])
    identifiers = list(dict.fromkeys([*_ARTICLE.findall(query), *_IDENTIFIER.findall(query)]))
    exact = sum(_literal_match(item, text) for item in identifiers)
    # A complete phrase is stronger evidence than unrelated shared n-grams.
    phrase = float(_literal_match(query, text))
    units = cjk_search_units(query)
    coverage = sum(unit in text for unit in units) / len(units) if units else 0.0
    return exact / len(identifiers) if identifiers else 0.0, phrase, coverage


def merge_ranked_hits(query: str, hits: list[dict], top_k: int = 5) -> list[dict]:
    """Globally rerank hybrid candidates; retain each collection's provenance.

    This function accepts only already-authorized API results. It performs
    no storage reads and cannot grant access. Duplicate chunk identities
    are removed within their collection, never across unrelated sources.
    """
    unique = {}
    for hit in hits:
        key = (hit["collection_id"], hit["chunk_id"])
        score = float(hit.get("score", 0.0))
        if not math.isfinite(score):
            raise _error(502, "rag_response_invalid", "檢索服務回傳無效的相關度分數")
        if key not in unique or score > float(unique[key].get("score", 0.0)):
            unique[key] = hit
    ranked = sorted(
        unique.values(),
        key=lambda hit: (
            *(-value for value in _lexical_key(query, hit)),
            -float(hit.get("score", 0.0)),
            hit["collection_id"], hit["document_id"], hit["chunk_id"],
        ),
    )
    return [deepcopy(hit) for hit in ranked[:top_k]]


def _annotate(hit, collection_id, *, requested_mode, retrieval_mode, reason=None):
    data = hit.model_dump()
    data["collection_id"] = collection_id
    data["metadata"] = {
        **(data.get("metadata") or {}),
        "collection_id": collection_id,
        "requested_mode": requested_mode,
        "retrieval_mode": retrieval_mode,
        "fulltext_fallback": reason is not None,
        "context_limited": False,
    }
    if reason is not None:
        data["metadata"]["fallback_reason"] = reason
    else:
        data["metadata"].pop("fallback_reason", None)
    # Surface citation locations already supplied by the parser, so the
    # flow's existing source formatter can read them without losing metadata.
    for name in ("page", "page_number", "slide", "line", "line_start", "line_end"):
        if name in data["metadata"]:
            data[name] = data["metadata"][name]
    if "line" not in data and "line_start" in data:
        data["line"] = data["line_start"]
    return data


def _fulltext_limit(hits: list[dict]) -> str | None:
    if len(hits) > MAX_CHUNKS:
        return "fulltext_chunk_limit"
    if len({(hit["collection_id"], hit["document_id"]) for hit in hits}) > MAX_DOCUMENTS:
        return "fulltext_document_limit"
    if sum(len(hit["content"]) + len(hit.get("parent_content") or "") for hit in hits) > MAX_CHARACTERS:
        return "fulltext_character_limit"
    if _size(hits) > MAX_CONTEXT_BYTES:
        return "fulltext_context_limit"
    return None


async def retrieve_collections(
    db, user, collection_ids: list[int], query: str, top_k: int = 5,
    *, mode: RetrievalMode = "hybrid",
) -> list[dict]:
    """Read complete small corpora or globally ranked hybrid context.

    Fulltext is atomic: if ANY corpus or aggregate bound is too large,
    discard that whole fulltext attempt and search ALL selected collections
    in hybrid mode. Returned metadata always states the actual mode.
    """
    if (mode not in {"hybrid", "fulltext"} or type(top_k) is not int
            or not 1 <= top_k <= 50 or not isinstance(collection_ids, list)
            or not 1 <= len(collection_ids) <= MAX_COLLECTIONS
            or any(type(item) is not int or item <= 0 for item in collection_ids)):
        raise _error(422, "rag_configuration_invalid", "請選擇 1 到 30 個文件集、有效模式及 1 到 50 個相關片段")
    # Construct the shared API schema before touching a model or store.
    search_api.SearchRequest(query=query, top_k=top_k, search_mode=mode)
    ids = list(dict.fromkeys(collection_ids))
    # Validate the WHOLE requested scope first. A later unauthorized source
    # must not cause embedding or storage calls to earlier collections.
    for collection_id in ids:
        coll = search_api._require_collection_access(db, user, collection_id, write=False)
        if coll.status != "active":
            raise _error(409, "rag_collection_inactive", "選取的文件集已停用，請重新選擇資料來源")
    principal = search_api.SearchPrincipal(user=user)
    reason = None
    if mode == "fulltext":
        complete = []
        for collection_id in ids:
            try:
                response = await search_api.search_collection(
                    collection_id, search_api.SearchRequest(query=query, search_mode="fulltext"),
                    db, principal,
                )
            except HTTPException as exc:
                if exc.status_code != 413:
                    raise
                reason = "fulltext_collection_limit"
                break
            complete.extend(_annotate(
                hit, collection_id, requested_mode=mode, retrieval_mode="fulltext",
            ) for hit in response.results)
            reason = _fulltext_limit(complete)
            if reason:
                break
        if reason is None:
            return complete

    candidates = []
    candidate_bytes = 0
    window = min(50, max(10, top_k * 3))
    for collection_id in ids:
        response = await search_api.search_collection(
            collection_id, search_api.SearchRequest(query=query, top_k=window, search_mode="hybrid"),
            db, principal,
        )
        batch = [_annotate(
            hit, collection_id, requested_mode=mode, retrieval_mode="hybrid", reason=reason,
        ) for hit in response.results]
        candidate_bytes += _size(batch)
        if candidate_bytes > MAX_CANDIDATE_BYTES or len(candidates) + len(batch) > MAX_CANDIDATES:
            raise _error(413, "rag_candidates_too_large", "檢索資料量超過上限，請縮小文件集範圍")
        candidates.extend(batch)
    ranked = merge_ranked_hits(query, candidates, len(candidates))
    selected = []
    limited = False
    for rank, hit in enumerate(ranked, start=1):
        hit["metadata"]["global_rank"] = rank
        if _size([*selected, hit]) > MAX_CONTEXT_BYTES:
            limited = True
            continue
        selected.append(hit)
        if len(selected) == top_k:
            break
    if ranked and not selected:
        raise _error(413, "rag_context_too_large", "單一檢索片段超過可用內容上限，請縮小文件或查詢範圍")
    if reason and not selected:
        raise _error(413, "rag_fulltext_fallback_empty", "文件超過全文上限，改用搜尋後仍無相關片段，請縮小文件範圍")
    if limited:
        for hit in selected:
            hit["metadata"]["context_limited"] = True
    return selected
