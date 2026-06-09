"""LLM-based cross-document relation extraction (document-relations Phase 2 / B).

The regex ``citation_extractor`` (Phase 1 / A) only catches explicitly written
citations. This extractor reads the parsed text + the collection's document
list and asks an LLM to emit edges that point **directly at a
``dst_document_id``** chosen from that list — so these edges are born resolved
and skip the fragile name-matching layer entirely.

Edges land in the SAME ``document_relations`` table with ``source='llm'`` and a
per-edge ``confidence``, coexisting with rule/manual rows (UNIQUE includes
source). Runs synchronously in ``ingest_document`` next to the regex step,
calling the LLM through the CSP ``/v1`` proxy (same channel + system key as the
embedder / VLM captioner).

Anti-hallucination (this is the whole risk of going LLM):
  * ``dst_document_id`` MUST be one of the candidate ids → fabricated targets
    are dropped.
  * ``evidence`` must be a verbatim-ish quote actually present in the source
    text → invented relations with no textual basis are dropped.
  * ``confidence`` is clamped to [0, 1]; retrieval expansion can threshold it.

The pure builder / parser are unit-tested; the asyncpg + httpx glue is
logic-reviewed (it needs a live LLM + PG, which the worker test harness can't
stand up deterministically).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Sequence

import httpx

logger = logging.getLogger(__name__)

_RELATION_TYPES = {
    "based_on", "amends", "supersedes", "cites", "supplements", "relates",
}
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class LlmEdge:
    """One validated LLM-extracted edge (target already resolved to a doc id)."""

    dst_document_id: int
    relation_type: str
    evidence: str
    confidence: float


_SYSTEM_PROMPT = (
    "你是一個法規/作業文件的關聯抽取助理。讀「本文」與「候選文件清單」,"
    "找出本文與清單中其他文件之間的關聯(例如:依據母法、補充、修正、廢止、引用)。\n"
    "規則:\n"
    "1. dst_document_id 一定要從候選清單的 id 挑,不可自創。\n"
    "2. evidence 必須是本文裡實際出現的原句片段(逐字),不可改寫或杜撰。\n"
    "3. relation_type 只能是:based_on / amends / supersedes / cites / supplements / relates。\n"
    "4. 沒有把握就不要輸出該筆;沒有任何關聯就輸出空陣列 []。\n"
    "5. 只輸出 JSON 陣列,不要任何說明文字。\n"
    "輸出格式:[{\"dst_document_id\": <int>, \"relation_type\": <str>, "
    "\"evidence\": <str>, \"confidence\": <0..1 float>}]"
)


def build_relation_messages(
    text: str,
    candidates: Sequence[tuple[int, str]],
    *,
    max_chars: int = 12000,
) -> list[dict[str, str]]:
    """Build the chat messages. ``candidates`` = ``[(document_id, title)]``."""
    listing = "\n".join(f"- id={cid}: {title}" for cid, title in candidates)
    body = (text or "")[:max_chars]
    user = (
        f"候選文件清單(只能從這些 id 挑 dst_document_id):\n{listing}\n\n"
        f"本文:\n{body}\n\n"
        "請輸出 JSON 陣列。"
    )
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def _extract_json_array(content: str) -> list[Any]:
    """Pull a JSON array out of an LLM reply, tolerating code fences / prose."""
    if not content:
        return []
    s = content.strip()
    # direct parse
    try:
        v = json.loads(s)
        if isinstance(v, list):
            return v
        if isinstance(v, dict):
            for key in ("relations", "edges", "data", "result"):
                if isinstance(v.get(key), list):
                    return v[key]
    except (ValueError, TypeError):
        pass
    # fallback: first '[' … last ']'
    i, j = s.find("["), s.rfind("]")
    if i != -1 and j != -1 and j > i:
        try:
            v = json.loads(s[i : j + 1])
            if isinstance(v, list):
                return v
        except (ValueError, TypeError):
            return []
    return []


def parse_llm_relations(
    content: str,
    *,
    candidate_ids: set[int],
    source_text: str,
    max_edges: int = 50,
    max_evidence: int = 500,
) -> list[LlmEdge]:
    """Validate the LLM reply into resolved edges (anti-hallucination guards).

    Drops any edge whose ``dst_document_id`` isn't a candidate, whose
    ``relation_type`` is unknown, or whose ``evidence`` isn't actually found in
    ``source_text``. Deduped by (dst, relation_type); capped at ``max_edges``.
    """
    items = _extract_json_array(content)
    collapsed_src = _WS_RE.sub("", source_text or "")
    out: list[LlmEdge] = []
    seen: set[tuple[int, str]] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        dst = item.get("dst_document_id")
        try:
            dst = int(dst)
        except (TypeError, ValueError):
            continue
        if dst not in candidate_ids:
            continue  # hallucinated target → drop
        rtype = item.get("relation_type")
        if rtype not in _RELATION_TYPES:
            continue
        evidence = str(item.get("evidence") or "").strip()[:max_evidence]
        if not evidence:
            continue
        # evidence must be grounded in the source text (collapsed-whitespace,
        # prefix match tolerates the model trimming a long quote).
        collapsed_ev = _WS_RE.sub("", evidence)
        probe = collapsed_ev[:16] if len(collapsed_ev) >= 16 else collapsed_ev
        if not probe or probe not in collapsed_src:
            continue  # ungrounded → drop
        key = (dst, rtype)
        if key in seen:
            continue
        seen.add(key)
        try:
            conf = float(item.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.5
        conf = max(0.0, min(1.0, conf))
        out.append(LlmEdge(dst, rtype, evidence, conf))
        if len(out) >= max_edges:
            break
    return out


async def _candidates(
    conn: Any, collection_id: int, exclude_id: int
) -> list[tuple[int, str, str]]:
    """``[(id, title_for_prompt, normalized_title)]`` for sibling docs."""
    rows = await conn.fetch(
        "SELECT id, title, normalized_title, filename FROM ingestion_documents "
        "WHERE collection_id = $1 AND id <> $2 AND status = 'indexed'",
        collection_id,
        exclude_id,
    )
    out: list[tuple[int, str, str]] = []
    for r in rows:
        title = r["title"] or r["filename"] or f"doc-{r['id']}"
        out.append((r["id"], title, r["normalized_title"] or ""))
    return out


async def _call_llm(messages: list[dict[str, str]], settings: Any) -> str:
    async with httpx.AsyncClient(
        base_url=settings.relation_llm_url,
        timeout=settings.relation_llm_timeout_seconds,
        verify=settings.relation_llm_verify_ssl,
        headers={"Authorization": f"Bearer {settings.relation_llm_api_key}"},
    ) as client:
        resp = await client.post(
            "/chat/completions",
            json={
                "model": settings.relation_llm_model,
                "messages": messages,
                "temperature": 0,
                "stream": False,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"] or ""


async def extract_and_resolve_llm(
    pool: Any,
    *,
    collection_id: int,
    document_id: int,
    text: str,
    run_id: str | None,
    settings: Any,
) -> dict[str, int]:
    """Ingest-time LLM extraction. Returns ``{"extracted": n}`` (0 when skipped).

    Gated by ``enable_relation_llm`` + a non-empty ``relation_llm_url``. Skips
    cleanly when there are no sibling documents or too many to list.
    """
    if not (settings.enable_relation_llm and settings.relation_llm_url):
        return {"extracted": 0}

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                f"SET LOCAL anila.collection_id = {int(collection_id)}"
            )
            cands = await _candidates(conn, collection_id, document_id)
            if not cands:
                return {"extracted": 0}
            if len(cands) > settings.relation_llm_max_candidates:
                logger.info(
                    "doc %s: %d sibling docs > cap %d — skipping LLM relation "
                    "extraction (needs a retrieval pre-filter)",
                    document_id, len(cands), settings.relation_llm_max_candidates,
                )
                return {"extracted": 0}

            norm_by_id = {cid: nt for cid, _t, nt in cands}
            messages = build_relation_messages(
                text, [(cid, t) for cid, t, _nt in cands],
                max_chars=settings.relation_llm_max_chars,
            )
            content = await _call_llm(messages, settings)
            edges = parse_llm_relations(
                content,
                candidate_ids=set(norm_by_id),
                source_text=text,
            )

            # delete-then-insert this doc's llm edges (rule/manual untouched)
            await conn.execute(
                "DELETE FROM document_relations "
                "WHERE collection_id = $1 AND src_document_id = $2 AND source = 'llm'",
                collection_id, document_id,
            )
            inserted = 0
            for e in edges:
                target_ref = norm_by_id.get(e.dst_document_id) or f"doc:{e.dst_document_id}"
                status = await conn.execute(
                    "INSERT INTO document_relations "
                    "(collection_id, src_document_id, dst_document_id, target_ref, "
                    " relation_type, confidence, source, extractor_run_id, evidence) "
                    "VALUES ($1, $2, $3, $4, $5, $6, 'llm', $7, $8) "
                    "ON CONFLICT (collection_id, src_document_id, target_ref, relation_type, source) "
                    "DO NOTHING",
                    collection_id, document_id, e.dst_document_id, target_ref,
                    e.relation_type, e.confidence, run_id, e.evidence,
                )
                if isinstance(status, str) and status.rsplit(" ", 1)[-1] == "1":
                    inserted += 1
    return {"extracted": inserted}
