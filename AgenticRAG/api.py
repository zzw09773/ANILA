"""OpenAI-compatible RAG API — port 24786

讓 OpenWebUI（或任何 OpenAI-compatible client）透過這個 API 使用
帶有 RAG 檢索的 LLM 對話。

流程：
  client → POST /v1/chat/completions
         → embed 最後一則 user message（NV-Embed-V2）
         → pgvector 檢索 top-k chunks
         → 注入 RAG context
         → 轉發至後端 LLM（google/gemma4）
         → stream 回傳 OpenAI 格式

啟動：
    python3 api.py
    uvicorn api:app --host 0.0.0.0 --port 24786
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

# Phase 2 Sprint 1 / Chunk F: AgenticRAG retrieves through the central
# anila-core SDK, not its own pg_pool / pgvector_store. The SDK enforces
# Layer 3 agent isolation (RLS via SET LOCAL anila.agent_id) so this
# template can't accidentally read another agent's chunks.
from anila_core.storage.adapters import AgentScopedPgVectorStore, PgPool
from anila_core.models.ingestion import SearchHit

from agentic_rag.api.middleware.loader import install_csp_middleware
from agentic_rag.ingestion.normalize import normalize_zh
from agentic_rag.ingestion.tokenize_zh import tokenize as _tokenize_query
from agentic_rag.providers.reranker import (
    RerankCandidate,
    Reranker,
    build_reranker_from_env,
)

# ── 載入 .env ─────────────────────────────────────────────────────────────────
load_dotenv(Path(__file__).parent / ".env")

# When deployed behind myCSPPlatform, LLM / Embedding calls are proxied
# through CSP. Set CSP_BASE_URL + CSP_API_KEY to activate; otherwise the
# direct LLM_URL / EMBEDDING_URL below are used (standalone mode).
_CSP_BASE_URL = os.getenv("CSP_BASE_URL", "").rstrip("/")
_CSP_API_KEY  = os.getenv("CSP_API_KEY",  "not-set")
_USE_CSP      = bool(_CSP_BASE_URL)

LLM_URL       = f"{_CSP_BASE_URL}/v1" if _USE_CSP else os.getenv("LLM_URL", "https://172.16.120.35/v1")
LLM_API_KEY   = _CSP_API_KEY          if _USE_CSP else os.getenv("LLM_API_KEY", "not-set")
EMB_URL       = f"{_CSP_BASE_URL}/v1" if _USE_CSP else os.getenv("EMBEDDING_URL", "https://172.16.120.35/v1")
EMB_API_KEY   = _CSP_API_KEY          if _USE_CSP else os.getenv("EMBEDDING_API_KEY", "not-set")

MODEL         = os.getenv("MODEL",              "google/gemma4")
EMB_MODEL     = os.getenv("EMBEDDING_MODEL",    "nvidia/nv-embed-v2")
DATABASE_URL  = os.getenv("DATABASE_URL",       "postgresql://csp_app:csp@localhost:5432/csp")
# Sprint 1 Chunk F: AgenticRAG is single-tenant per deployment — the
# host platform launches one container per agent and pins this env. The
# value flows into AgentScopedPgVectorStore at startup and every
# retrieval inherits its RLS scope from there. No per-request agent
# selection at this layer (Sprint 2's multi-tenant routing handles that).
RAG_AGENT_ID  = int(os.getenv("RAG_AGENT_ID",   "0"))
RAG_TOP_K     = int(os.getenv("RAG_TOP_K",     "5"))
RAG_MIN_SCORE = float(os.getenv("RAG_MIN_SCORE", "0.5"))
RAG_MIN_SCORE_RETRY = float(os.getenv("RAG_MIN_SCORE_RETRY", "0.3"))
RAG_RERANK_POOL_MULTIPLIER = int(os.getenv("RAG_RERANK_POOL_MULTIPLIER", "3"))
VERIFY_SSL    = os.getenv("EMBEDDING_VERIFY_SSL", "false").lower() == "true"

# Service-to-service token issued by myCSPPlatform. When empty the CSP
# middleware runs in pass-through dev mode.
_CSP_SERVICE_TOKEN = os.getenv("CSP_SERVICE_TOKEN") or None

SYSTEM_PROMPT = os.getenv("RAG_SYSTEM_PROMPT", """你是一個知識檢索助手。請根據提供的參考資料回答用戶問題。

## 回覆原則
- 優先使用 [RAG Context] 中的資料回答，並標注來源文件名稱
- 如果參考資料不足以回答，誠實說明並提供你所知道的資訊
- 使用繁體中文回覆，除非用戶使用其他語言
- 回覆要簡潔、結構化，適當使用列表或標題
""".strip())

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("rag-api")

# ── DB pool + RAG store + Reranker ────────────────────────────────────────────
# Sprint 1 Chunk F: replaced raw asyncpg pool with anila_core's PgPool.
# AgentScopedPgVectorStore is the only retrieval entry point; it pins
# RAG_AGENT_ID into ``anila.agent_id`` per-connection so RLS auto-scopes.
_pool: PgPool | None = None
_store: AgentScopedPgVectorStore | None = None
_reranker: Reranker | None = None

# halfvec(4000) is the central schema (CSP migration 0015). NV-Embed-V2
# returns 4096-d; we truncate to 4000 client-side because the embedding
# proxy ignores OpenAI's ``dimensions`` parameter. Drop the tail 96 dims
# — Matryoshka-trained NV-Embed-V2 keeps near-full quality at 4000.
_EMBEDDING_DIM = 4000


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global _pool, _store, _reranker
    if RAG_AGENT_ID <= 0:
        logger.warning(
            "RAG_AGENT_ID is %d — RAG disabled. Set a positive int per "
            "the agent this AgenticRAG container serves.",
            RAG_AGENT_ID,
        )
    else:
        try:
            _pool = PgPool(DATABASE_URL, min_size=2, max_size=8)
            await _pool.open()
            _store = AgentScopedPgVectorStore(_pool, agent_id=RAG_AGENT_ID)
            logger.info(
                "PostgreSQL pool ready; AgentScopedPgVectorStore "
                "scoped to agent_id=%d",
                RAG_AGENT_ID,
            )
        except Exception as e:
            logger.warning("PostgreSQL unavailable (%s) — RAG disabled", e)
            _pool = None
            _store = None
    try:
        _reranker = build_reranker_from_env()
        if _reranker is not None:
            logger.info("Reranker enabled: %s", type(_reranker).__name__)
    except Exception as e:
        logger.warning("Reranker init failed (%s) — disabled", e)
        _reranker = None
    yield
    if _pool:
        await _pool.close()

app = FastAPI(title="AgenticRAG API", version="1.0.0", lifespan=lifespan)

# CSP service-to-service auth middleware. When running inside the ANILA
# platform this loads from anila-core; standalone deployments fall back to
# the in-package copy at agentic_rag.api.middleware.csp_auth. Either way
# the behaviour is the same: require X-CSP-Service-Token on /v1/* when
# CSP_SERVICE_TOKEN is set, pass-through when unset.
_csp_loaded_from = install_csp_middleware(app, _CSP_SERVICE_TOKEN)
logger.info(
    "CSP middleware: loaded_from=%s, enforced=%s, csp_proxy_mode=%s",
    _csp_loaded_from,
    bool(_CSP_SERVICE_TOKEN),
    _USE_CSP,
)


# ── /v1/models ────────────────────────────────────────────────────────────────

@app.get("/v1/models")
async def list_models() -> JSONResponse:
    return JSONResponse({
        "object": "list",
        "data": [{
            "id":       f"rag/{MODEL}",
            "object":   "model",
            "created":  int(time.time()),
            "owned_by": "agentic-rag",
        }],
    })


# ── /v1/chat/completions ──────────────────────────────────────────────────────

@app.post("/v1/chat/completions", response_model=None)
async def chat_completions(request: Request) -> StreamingResponse | JSONResponse:
    body: dict = await request.json()
    messages: list[dict] = body.get("messages", [])
    stream: bool = body.get("stream", False)
    # 忽略 client 送來的 model 名稱（OpenWebUI 會送連線名稱而非真實 model ID）
    # 永遠使用 .env 設定的 MODEL
    model: str = MODEL
    logger.info("REQUEST stream=%s client_model=%s → actual=%s",
                stream, body.get("model"), model)

    # 1. RAG：檢索相關 chunks
    rag_context, rag_trace, rag_sources = await retrieve_context(messages)
    if rag_context:
        messages = inject_context(messages, rag_context)

    # 2. 注入 system prompt（如果 client 沒有帶 system message）
    if SYSTEM_PROMPT:
        has_system = any(m.get("role") == "system" for m in messages)
        if not has_system:
            messages = [{"role": "system", "content": SYSTEM_PROMPT}] + messages

    # 3. 組裝轉發給 LLM 的 payload（強制 stream=True，內部統一用 stream 收）
    payload = {**body, "model": model, "messages": messages, "stream": True}
    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type":  "application/json",
    }

    if stream:
        return StreamingResponse(
            _stream_with_rag_trace(payload, headers, rag_trace, rag_sources),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    else:
        full = await _collect_stream(payload, headers, rag_sources)
        return JSONResponse(full)


async def _stream_with_rag_trace(
    payload: dict, headers: dict, rag_trace: str, rag_sources: str
) -> AsyncIterator[str]:
    """先 yield RAG 檢索軌跡到 thinking block，再串流 LLM 回應，最後附來源清單。"""
    if rag_trace:
        # 在 thinking block 最前面插入 RAG 來源資訊
        trace_chunk = {
            "id": f"rag-trace-{uuid.uuid4().hex[:8]}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": payload["model"],
            "choices": [{"index": 0, "delta": {"reasoning_content": rag_trace}}],
        }
        yield "data: " + json.dumps(trace_chunk, ensure_ascii=False) + "\n\n"

    async for chunk in _forward_stream(payload, headers, rag_sources):
        yield chunk


async def _forward_stream(
    payload: dict, headers: dict, rag_sources: str = ""
) -> AsyncIterator[str]:
    """Proxy SSE 串流給 client，並將 delta.reasoning 轉為
    OpenWebUI 能顯示的 delta.reasoning_content（thinking block）。
    若有 rag_sources，在 [DONE] 前插入來源清單 chunk。"""
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:8]}"
    async with httpx.AsyncClient(verify=VERIFY_SSL, timeout=120) as client:
        async with client.stream(
            "POST", f"{LLM_URL}/chat/completions",
            json=payload, headers=headers,
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line:
                    continue
                if not line.startswith("data: "):
                    yield line + "\n\n"
                    continue
                data_str = line[6:]
                if data_str == "[DONE]":
                    # 在 [DONE] 前插入來源清單
                    if rag_sources:
                        src_chunk = {
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": payload["model"],
                            "choices": [{"index": 0, "delta": {"content": rag_sources},
                                         "finish_reason": None}],
                        }
                        yield "data: " + json.dumps(src_chunk, ensure_ascii=False) + "\n\n"
                    yield "data: [DONE]\n\n"
                    continue
                try:
                    chunk = json.loads(data_str)
                    # 記錄 completion id 以便來源 chunk 對齊
                    if chunk.get("id"):
                        completion_id = chunk["id"]
                    # 將 delta.reasoning → delta.reasoning_content
                    # 讓 OpenWebUI 顯示為可折疊的 thinking block
                    for choice in chunk.get("choices", []):
                        delta = choice.get("delta", {})
                        if "reasoning" in delta:
                            delta["reasoning_content"] = delta.pop("reasoning")
                    yield "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"
                except (json.JSONDecodeError, KeyError):
                    yield line + "\n\n"


async def _collect_stream(payload: dict, headers: dict, rag_sources: str = "") -> dict:
    """收集 stream 回應，組裝成完整 ChatCompletion 物件。"""
    content = ""
    finish_reason = "stop"
    usage: dict = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    async with httpx.AsyncClient(verify=VERIFY_SSL, timeout=120) as client:
        async with client.stream(
            "POST", f"{LLM_URL}/chat/completions",
            json=payload, headers=headers,
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                    delta = chunk["choices"][0].get("delta", {})
                    content += delta.get("content") or ""
                    if chunk["choices"][0].get("finish_reason"):
                        finish_reason = chunk["choices"][0]["finish_reason"]
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                except Exception:
                    pass

    if rag_sources:
        content += rag_sources

    return {
        "id":      f"chatcmpl-{uuid.uuid4().hex[:8]}",
        "object":  "chat.completion",
        "created": int(time.time()),
        "model":   payload["model"],
        "choices": [{"index": 0,
                     "message": {"role": "assistant", "content": content},
                     "finish_reason": finish_reason}],
        "usage":   usage,
    }


# ── RAG helpers ───────────────────────────────────────────────────────────────

async def _vector_search(embedding: list[float]) -> list[SearchHit]:
    """Semantic search via central SDK (RLS-scoped to RAG_AGENT_ID).

    Two-tier threshold: try ``RAG_MIN_SCORE`` first; if no hits, retry
    with the lower ``RAG_MIN_SCORE_RETRY``. Lets us keep precision in
    the common case but degrade gracefully on rare queries where the
    corpus has only weak matches.
    """
    if _store is None:
        return []
    hits = await _store.similarity_search(
        embedding, top_k=RAG_TOP_K, min_score=RAG_MIN_SCORE
    )
    if not hits and RAG_MIN_SCORE_RETRY < RAG_MIN_SCORE:
        logger.info(
            "RAG vector: no results at %.2f, retry at %.2f",
            RAG_MIN_SCORE, RAG_MIN_SCORE_RETRY,
        )
        hits = await _store.similarity_search(
            embedding, top_k=RAG_TOP_K, min_score=RAG_MIN_SCORE_RETRY
        )
    return hits


def _expand_tokens(query: str) -> list[str]:
    """產生多種關鍵字變體，處理中文法條格式的空格問題。

    例：「第8條」→ 也加入「第 8 條」（PDF 解析後常在每字間加空格）
    """
    base = query.strip()
    variants: set[str] = {base}

    # 拆出個別 token（空白分隔）
    for t in base.split():
        if len(t) > 1:
            variants.add(t)
            # 字元間插入空格版：「第8條」→「第 8 條」
            spaced = " ".join(t)
            variants.add(spaced)

    # 整句也加字元間空格版
    variants.add(" ".join(base))
    return list(variants)


async def _keyword_search(query: str) -> list[SearchHit]:
    """Keyword search via central SDK FTS path.

    Pre-tokenises CJK with the existing ``tokenize_zh`` so plainto_tsquery
    sees space-separated tokens (PG's default tokenizer doesn't split
    Chinese). When FTS returns nothing the SDK simply yields an empty
    list; the previous ILIKE-fallback path is dropped because it no
    longer compiles against the new schema (chunk_type column gone)
    and hybrid RRF tolerates zero keyword hits gracefully — vector
    search alone still produces results.
    """
    if _store is None:
        return []
    tokenized = _tokenize_query(query) if query else None
    try:
        return await _store.keyword_search(
            query=query,
            tokenized_query=tokenized or None,
            top_k=RAG_TOP_K * 2,
        )
    except Exception as exc:
        logger.warning("Keyword search failed (%s) — vector path only", exc)
        return []


def _rrf_merge(
    vec_hits: list[SearchHit],
    kw_hits: list[SearchHit],
    top_k: int,
    k: int = 60,
) -> list[dict]:
    """Reciprocal Rank Fusion across central-SDK SearchHits.

    Both inputs are ``list[SearchHit]`` from anila_core. We key by
    ``chunk.id`` (BIGINT, schema-level unique) — the legacy ``chunk_id``
    name is preserved on the output dict for downstream compatibility
    (reranker / context builder still read ``r["chunk_id"]``).

    score = Σ 1/(k + rank); k=60 standard.
    """
    rrf: dict[int, float] = {}
    data: dict[int, dict] = {}

    for rank, hit in enumerate(vec_hits, 1):
        cid = hit.chunk.id
        rrf[cid] = rrf.get(cid, 0.0) + 1.0 / (k + rank)
        data[cid] = {
            "chunk_id":  cid,
            "content":   hit.chunk.content,
            "metadata":  hit.chunk.metadata,
            "vec_score": float(hit.score),
            "kw_match":  False,
        }

    for rank, hit in enumerate(kw_hits, 1):
        cid = hit.chunk.id
        rrf[cid] = rrf.get(cid, 0.0) + 1.0 / (k + rank)
        if cid not in data:
            data[cid] = {
                "chunk_id":  cid,
                "content":   hit.chunk.content,
                "metadata":  hit.chunk.metadata,
                "vec_score": None,
                "kw_match":  True,
            }
        else:
            data[cid]["kw_match"] = True

    sorted_ids = sorted(rrf, key=lambda x: rrf[x], reverse=True)[:top_k]
    return [{**data[cid], "rrf_score": rrf[cid]} for cid in sorted_ids]


async def retrieve_context(messages: list[dict]) -> tuple[str, str, str]:
    """Hybrid Search（語意 + 關鍵字）後回傳
    (context_for_llm, trace_for_thinking_block, sources_for_reply)。
    """
    if _store is None:
        return "", "", ""
    raw_query = _last_user_text(messages)
    if not raw_query:
        return "", "", ""
    query = normalize_zh(raw_query)

    # 1. Embed query（語意搜尋用）
    try:
        async with httpx.AsyncClient(verify=VERIFY_SSL, timeout=30) as client:
            resp = await client.post(
                f"{EMB_URL}/embeddings",
                headers={"Authorization": f"Bearer {EMB_API_KEY}",
                         "Content-Type": "application/json"},
                json={"model": EMB_MODEL, "input": [query], "input_type": "query"},
            )
            resp.raise_for_status()
            embedding: list[float] = resp.json()["data"][0]["embedding"]
    except Exception as e:
        logger.warning("Embedding failed: %s", e)
        return "", "", ""

    # Truncate to halfvec(4000) schema dim. NV-Embed-V2 native 4096-d
    # → keep first 4000. Must match what the ingestion-worker stored.
    if len(embedding) > _EMBEDDING_DIM:
        embedding = embedding[:_EMBEDDING_DIM]

    # 2. 並行執行語意搜尋 + 關鍵字搜尋
    import asyncio as _asyncio
    try:
        vec_rows, kw_rows = await _asyncio.gather(
            _vector_search(embedding),
            _keyword_search(query),
        )
    except Exception as e:
        logger.warning("Search failed: %s", e)
        return "", "", ""

    # 3. RRF merge — fetch wider pool when reranker is available
    pool_size = (
        RAG_TOP_K * RAG_RERANK_POOL_MULTIPLIER if _reranker is not None else RAG_TOP_K
    )
    merged = _rrf_merge(vec_rows, kw_rows, pool_size)
    if not merged:
        return "", "", ""

    # 3b. Optional reranker — cross-encoder over (query, content) pairs
    if _reranker is not None and len(merged) > 1:
        try:
            candidates = [
                RerankCandidate(
                    chunk_id=str(r.get("chunk_id") or i),
                    content=r["content"],
                    metadata=r["metadata"] or {},
                    original_score=r.get("rrf_score"),
                )
                for i, r in enumerate(merged)
            ]
            reranked = await _reranker.rerank(query, candidates, top_k=RAG_TOP_K)
            if reranked:
                by_cid = {
                    str(r.get("chunk_id") or i): r for i, r in enumerate(merged)
                }
                merged = [
                    {**by_cid[item.candidate.chunk_id], "rerank_score": item.score}
                    for item in reranked
                    if item.candidate.chunk_id in by_cid
                ]
        except Exception as exc:
            logger.warning("Reranker call failed (%s) — using RRF order", exc)
            merged = merged[:RAG_TOP_K]
    else:
        merged = merged[:RAG_TOP_K]

    # 4. 組裝輸出
    ctx_lines   = ["[RAG Context - Retrieved Documents]"]
    trace_lines = ["🔍 RAG 檢索結果（Hybrid Search）："]
    src_lines:  list[str] = []
    has_high_score = any(
        r["vec_score"] is not None and r["vec_score"] >= RAG_MIN_SCORE
        for r in merged
    )

    for i, r in enumerate(merged, 1):
        meta = r["metadata"] or {}
        if isinstance(meta, str):
            meta = json.loads(meta)
        source = (
            meta.get("filename")
            or meta.get("source_path", "").rsplit("/", 1)[-1]
            or "unknown"
        )
        vec_score = r["vec_score"]
        kw_match  = r["kw_match"]
        rrf_score = r["rrf_score"]

        # 標注匹配方式
        if kw_match and vec_score is not None:
            match_tag = f"語意 {vec_score:.3f} + 關鍵字"
        elif kw_match:
            match_tag = "關鍵字匹配"
        else:
            match_tag = f"語意 {vec_score:.3f}"

        ctx_lines.append(f"--- Source {i} ({match_tag}, doc: {source}) ---")
        ctx_lines.append(r["content"])
        trace_lines.append(f"  [{i}] {source}  {match_tag}  RRF={rrf_score:.4f}")
        trace_lines.append(f"       {r['content'][:80].strip()}…")

        # 來源清單：keyword-only 或低語意分的加標注
        if kw_match and (vec_score is None or vec_score < RAG_MIN_SCORE):
            src_lines.append(f"{i}. **{source}**（{match_tag}）")
        elif vec_score is not None and vec_score >= RAG_MIN_SCORE:
            src_lines.append(f"{i}. **{source}**（相似度 {vec_score:.3f}）")
        else:
            src_lines.append(f"{i}. {source}（相似度 {vec_score:.3f}，低相關度）")

    ctx_lines.append("[End RAG Context]")
    trace_lines.append("")

    src_header = "**參考來源：**" if has_high_score else "**參考來源**（相關度較低，僅供參考）："
    sources_str = ("\n\n---\n" + src_header + "\n" + "\n".join(src_lines)) if src_lines else ""

    logger.info("RAG hybrid: vec=%d kw=%d merged=%d query=%.50r",
                len(vec_rows), len(kw_rows), len(merged), query)
    return "\n".join(ctx_lines), "\n".join(trace_lines), sources_str


def inject_context(messages: list[dict], context: str) -> list[dict]:
    result = list(messages)
    for i in range(len(result) - 1, -1, -1):
        if result[i].get("role") == "user":
            orig = result[i]["content"]
            if isinstance(orig, str):
                result[i] = {**result[i], "content": f"{context}\n\n{orig}"}
            elif isinstance(orig, list):
                result[i] = {**result[i], "content":
                             [{"type": "text", "text": context + "\n\n"}] + orig}
            return result
    return result


def _last_user_text(messages: list[dict]) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "user":
            c = msg.get("content", "")
            if isinstance(c, str):
                return c.strip()
            if isinstance(c, list):
                return " ".join(
                    p.get("text", "") for p in c
                    if isinstance(p, dict) and p.get("type") == "text"
                ).strip()
    return ""


# ── 健康檢查 ──────────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "model": MODEL, "rag": _pool is not None}


# ── 入口 ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=24786, log_level="info")
