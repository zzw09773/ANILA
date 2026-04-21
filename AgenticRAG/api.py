"""AgenticRAG — Sample Agent endpoint (OpenAI-compatible).

這是 ANILA 平台的 **RAG 範例 agent 樣板**。Fork 本 repo 後，主要要動的就是
這個檔案（以及 ``index_documents.py``）。

Port: 24786 — 對 CSP / Router 暴露 OpenAI-compat 介面。

預設流程：
  client → POST /v1/chat/completions
         → CspServiceTokenMiddleware 驗 s2s token
         → embed 最後一則 user message（NV-Embed-V2）
         → pgvector 語意 + ILIKE 關鍵字 hybrid 檢索 + RRF 融合
         → 注入 RAG context 到 messages
         → 轉發至後端 LLM（直連 or 透過 CSP proxy）
         → SSE 串流回傳 OpenAI 格式 + thinking block + 來源清單

Fork 時要改哪些：
  - ``retrieve_context()`` — 整個檢索邏輯。換成你的 knowledge backend。
  - ``SYSTEM_PROMPT`` — 你的 agent 人格與回覆格式要求。
  - ``_forward_stream()`` / ``_collect_stream()`` — 若 LLM backend 非 OpenAI-compat。
  - ``lifespan()`` — 開關 pgvector 連線；改為你要的資源（Redis / Milvus / API client）。

啟動：
    python3 api.py
    # 或
    uvicorn api:app --host 0.0.0.0 --port 24786

更多細節見根 README.md 的「Fork → 部署 流程」章節。
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

# ── 載入 .env ─────────────────────────────────────────────────────────────────
load_dotenv(Path(__file__).parent / ".env")

# When deployed behind CSP, LLM/Embedding calls go through CSP proxy.
# Set CSP_BASE_URL + CSP_API_KEY in env to activate; fallback to direct URLs.
_CSP_BASE_URL = os.getenv("CSP_BASE_URL", "").rstrip("/")
_CSP_API_KEY  = os.getenv("CSP_API_KEY",  "not-set")
_USE_CSP      = bool(_CSP_BASE_URL)

LLM_URL       = f"{_CSP_BASE_URL}/v1" if _USE_CSP else os.getenv("LLM_URL", "https://172.16.120.35/v1")
LLM_API_KEY   = _CSP_API_KEY          if _USE_CSP else os.getenv("LLM_API_KEY", "not-set")
EMB_URL       = f"{_CSP_BASE_URL}/v1" if _USE_CSP else os.getenv("EMBEDDING_URL", "https://172.16.120.35/v1")
EMB_API_KEY   = _CSP_API_KEY          if _USE_CSP else os.getenv("EMBEDDING_API_KEY", "not-set")

MODEL         = os.getenv("MODEL",              "google/gemma4")
EMB_MODEL     = os.getenv("EMBEDDING_MODEL",    "nvidia/nv-embed-v2")
DATABASE_URL  = os.getenv("DATABASE_URL",       "postgresql://anila:anila@localhost:5432/anila_rag")
RAG_TOP_K     = int(os.getenv("RAG_TOP_K",     "5"))
RAG_MIN_SCORE = float(os.getenv("RAG_MIN_SCORE", "0.5"))
RAG_MIN_SCORE_RETRY = float(os.getenv("RAG_MIN_SCORE_RETRY", "0.3"))
VERIFY_SSL    = os.getenv("EMBEDDING_VERIFY_SSL", "false").lower() == "true"

# Service-to-service token injected by CSP; None = auth disabled (local dev)
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

# ── DB pool ───────────────────────────────────────────────────────────────────
_pool = None

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global _pool
    try:
        import asyncpg  # type: ignore
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=8)
        logger.info("PostgreSQL pool ready")
    except Exception as e:
        logger.warning("PostgreSQL unavailable (%s) — RAG disabled", e)
    yield
    if _pool:
        await _pool.close()

app = FastAPI(title="ANILA RAG API", version="1.0.0", lifespan=lifespan)

# Add CSP service-token auth middleware.
# SECURITY: when CSP_SERVICE_TOKEN is set, the middleware MUST load. An import
# failure would otherwise silently drop auth and expose the agent endpoint. So
# we only tolerate the ImportError fallthrough when no token is configured
# (explicit local-dev mode).
_middleware_loaded = False
for _import_path in (
    "anila_core.api.middleware.auth",
    "src.anila_core.api.middleware.auth",
):
    try:
        _mod = __import__(_import_path, fromlist=["CspServiceTokenMiddleware"])
        CspServiceTokenMiddleware = _mod.CspServiceTokenMiddleware
        app.add_middleware(
            CspServiceTokenMiddleware,
            service_token=_CSP_SERVICE_TOKEN,
            dev_mode=(not _CSP_SERVICE_TOKEN),
        )
        _middleware_loaded = True
        break
    except ImportError:
        continue

if not _middleware_loaded:
    if _CSP_SERVICE_TOKEN:
        raise RuntimeError(
            "CspServiceTokenMiddleware failed to import but CSP_SERVICE_TOKEN is "
            "set. Refusing to start the agent unauthenticated. Check the "
            "`anila_core` package is installed / on PYTHONPATH before running."
        )
    logger.warning(
        "CspServiceTokenMiddleware not available and no CSP_SERVICE_TOKEN set — "
        "running without service-to-service auth (local dev only)."
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
            "owned_by": "anila-core",
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

    # 3. 組裝轉發給 LLM 的 payload
    payload = {**body, "model": model, "messages": messages, "stream": stream}
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
    if _USE_CSP:
        response = await _post_chat_completion({**payload, "stream": False}, headers)
        completion_id = response.get("id", completion_id)
        content = response["choices"][0]["message"]["content"]
        usage = response.get("usage")
        chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": payload["model"],
            "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
        }
        yield "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"
        if rag_sources:
            src_chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": payload["model"],
                "choices": [{"index": 0, "delta": {"content": rag_sources}, "finish_reason": None}],
            }
            yield "data: " + json.dumps(src_chunk, ensure_ascii=False) + "\n\n"
        final_chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": payload["model"],
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        if usage:
            final_chunk["usage"] = usage
        yield "data: " + json.dumps(final_chunk, ensure_ascii=False) + "\n\n"
        yield "data: [DONE]\n\n"
        return

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
    if _USE_CSP:
        result = await _post_chat_completion({**payload, "stream": False}, headers)
        if rag_sources:
            result["choices"][0]["message"]["content"] += rag_sources
        return result

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


async def _post_chat_completion(payload: dict, headers: dict) -> dict:
    async with httpx.AsyncClient(verify=VERIFY_SSL, timeout=120) as client:
        resp = await client.post(
            f"{LLM_URL}/chat/completions",
            json=payload,
            headers=headers,
        )
        resp.raise_for_status()
        return resp.json()


# ═════════════════════════════════════════════════════════════════════════════
# FORK ME ↓↓↓ — 以下是範例 RAG 實作。
# 你要把這整塊換成自己的檢索邏輯（外部 API / 另一個模型 / 自建 index 等）。
# 唯一的合約：``retrieve_context(messages)`` 必須回傳
#   (context_for_llm: str, trace_for_thinking_block: str, sources_for_reply: str)
# 三個字串。前兩個是 thinking block / 注入訊息用，最後一個是回覆末尾來源清單。
# ═════════════════════════════════════════════════════════════════════════════

# ── RAG helpers ───────────────────────────────────────────────────────────────

async def _vector_search(vec_str: str) -> list:
    """pgvector 語意搜尋，含低門檻 retry。回傳 asyncpg Record list（含 score 欄）。"""
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT chunk_id, content, metadata,
                   1 - (embedding <=> $1::vector) AS score
            FROM document_chunks
            WHERE embedding IS NOT NULL
              AND 1 - (embedding <=> $1::vector) >= $2
            ORDER BY embedding <=> $1::vector
            LIMIT $3
            """,
            vec_str, RAG_MIN_SCORE, RAG_TOP_K,
        )
        if not rows and RAG_MIN_SCORE_RETRY < RAG_MIN_SCORE:
            logger.info("RAG vector: no results at %.2f, retry at %.2f",
                        RAG_MIN_SCORE, RAG_MIN_SCORE_RETRY)
            rows = await conn.fetch(
                """
                SELECT chunk_id, content, metadata,
                       1 - (embedding <=> $1::vector) AS score
                FROM document_chunks
                WHERE embedding IS NOT NULL
                  AND 1 - (embedding <=> $1::vector) >= $2
                ORDER BY embedding <=> $1::vector
                LIMIT $3
                """,
                vec_str, RAG_MIN_SCORE_RETRY, RAG_TOP_K,
            )
    return list(rows)


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


async def _keyword_search(query: str) -> list:
    """ILIKE 關鍵字搜尋：對 query 本身及各種變體做 substring match。
    處理 PDF 解析後字間有空格的情況（如「第 8 條」vs「第8條」）。
    回傳 asyncpg Record list（無 score 欄）。
    """
    tokens = _expand_tokens(query)
    conditions = " OR ".join(f"content ILIKE ${i + 1}" for i in range(len(tokens)))
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT chunk_id, content, metadata
            FROM document_chunks
            WHERE embedding IS NOT NULL
              AND ({conditions})
            LIMIT ${len(tokens) + 1}
            """,
            *[f"%{t}%" for t in tokens],
            RAG_TOP_K * 2,
        )
    return list(rows)


def _rrf_merge(
    vec_rows: list,
    kw_rows: list,
    top_k: int,
    k: int = 60,
) -> list[dict]:
    """Reciprocal Rank Fusion：合併語意搜尋與關鍵字搜尋結果。

    score = Σ 1/(k + rank)，k=60 為標準值。
    回傳 list[dict]，每筆包含 content / metadata / vec_score / kw_rank / rrf_score。
    """
    rrf: dict[str, float] = {}
    data: dict[str, dict] = {}

    for rank, row in enumerate(vec_rows, 1):
        cid = row["chunk_id"]
        rrf[cid] = rrf.get(cid, 0.0) + 1.0 / (k + rank)
        data[cid] = {
            "content":   row["content"],
            "metadata":  row["metadata"],
            "vec_score": float(row["score"]),
            "kw_match":  False,
        }

    for rank, row in enumerate(kw_rows, 1):
        cid = row["chunk_id"]
        rrf[cid] = rrf.get(cid, 0.0) + 1.0 / (k + rank)
        if cid not in data:
            data[cid] = {
                "content":   row["content"],
                "metadata":  row["metadata"],
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
    if _pool is None:
        return "", "", ""
    query = _last_user_text(messages)
    if not query:
        return "", "", ""

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

    # 2. 並行執行語意搜尋 + 關鍵字搜尋
    import asyncio as _asyncio
    vec_str = "[" + ",".join(str(v) for v in embedding) + "]"
    try:
        vec_rows, kw_rows = await _asyncio.gather(
            _vector_search(vec_str),
            _keyword_search(query),
        )
    except Exception as e:
        logger.warning("Search failed: %s", e)
        return "", "", ""

    # 3. RRF merge
    merged = _rrf_merge(vec_rows, kw_rows, RAG_TOP_K)
    if not merged:
        return "", "", ""

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


# ═════════════════════════════════════════════════════════════════════════════
# FORK ME ↑↑↑ — 以上為範例 RAG 實作結束
# ═════════════════════════════════════════════════════════════════════════════


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
