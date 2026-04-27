"""LLM-as-judge scoring for the Chunking Evaluator.

Sprint 5 / Chunk X. Per docs/ingestion-platform-design.md §6.5:

    Hit@1 / Hit@5 / MRR are pure retrieval metrics — they only ask
    "did we surface the right doc?". They don't ask "is the chunk
    content actually useful?". For prose / explanation queries that
    second axis matters more — a chunk can be in the top-5 but still
    be useless context for the LLM.

For each (query, retrieved chunks) pair, we ask a judge LLM to score
1–3 for relevance:

    1 = irrelevant (chunks don't help answer)
    2 = partially relevant (some help, but missing key info)
    3 = directly answers (LLM could give a useful response from this)

Scores get averaged per strategy → ``judge_avg`` metric.

The judge LLM credential lives in ``user_llm_credentials`` (Chunk L);
the AES-encrypted API key is decrypted just-in-time per call and the
decrypted key never persists beyond the outbound httpx call.

Cost / throttle (design doc §6.5):
- 50 queries × 6 strategies = 300 judge calls per run.
- gpt-4o-mini ~$0.001/call → ~$0.30/run. Manageable.
- Sprint 5 X first cut: no caching / throttling. Sprint 6 will add
  per-(query, chunk-set) cache + Fast/Standard/Deep budget gates.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import httpx

from anila_core.storage.adapters.pg_pool import PgPool


logger = logging.getLogger(__name__)


_PROMPT_TEMPLATE = """你是一個檢索結果評分助理。根據下面的 query 跟檢索到的 chunks，給整組 chunks 一個 1–3 分的相關性評分：

1 = 答非所問（chunks 不能回答 query）
2 = 部分相關（chunks 提供片段資訊但缺關鍵內容）
3 = 直接命中（LLM 可以從這些 chunks 直接給出有用回答）

只輸出一個阿拉伯數字，不要任何文字解釋。

Query: {query}

Retrieved chunks:
{chunks_block}

Score (1, 2, or 3):"""


_SCORE_RE = re.compile(r"^\s*([1-3])\b")


@dataclass(frozen=True)
class JudgeCredential:
    """Decrypted credential ready for outbound use.

    Built from a ``user_llm_credentials`` row + ``decrypt_credential``.
    Frozen so call sites can't accidentally mutate the API key string.
    """

    endpoint_url: str
    model_name: str
    api_key: str


async def load_judge_credential(
    pool: PgPool, credential_id: int
) -> JudgeCredential:
    """Fetch + decrypt one credential row.

    Raises ``LookupError`` when the row doesn't exist; the call site
    should swallow + record on the eval_run as a soft failure.
    """
    # Central anila-core decrypt helper. Same crypto as CSP's create
    # path so encrypt(at CSP) → decrypt(at worker) round-trips.
    from anila_core.security import decrypt_credential

    sql = """
        SELECT endpoint_url, model_name,
               api_key_encrypted, api_key_nonce, api_key_tag
          FROM user_llm_credentials
         WHERE id = $1
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(sql, credential_id)
    if row is None:
        raise LookupError(f"user_llm_credential {credential_id} not found")
    plaintext = decrypt_credential(
        bytes(row["api_key_encrypted"]),
        bytes(row["api_key_nonce"]),
        bytes(row["api_key_tag"]),
    )
    return JudgeCredential(
        endpoint_url=row["endpoint_url"],
        model_name=row["model_name"],
        api_key=plaintext,
    )


async def score_one(
    cred: JudgeCredential,
    query: str,
    chunk_contents: list[str],
    *,
    timeout_s: float = 30.0,
) -> int | None:
    """Run one judge call. Returns 1/2/3, or None on parse failure.

    Failures are intentionally non-fatal: the evaluator averages over
    the remaining successful scores. A consistently-failing judge LLM
    surfaces as a per-strategy ``judge_avg`` of None / NaN, which the
    UI displays as "—" rather than crashing the run.
    """
    if not chunk_contents:
        return None

    chunks_block = "\n\n".join(
        f"[chunk {i + 1}]\n{c}" for i, c in enumerate(chunk_contents)
    )
    prompt = _PROMPT_TEMPLATE.format(query=query, chunks_block=chunks_block)

    body = {
        "model": cred.model_name,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,  # deterministic
        "max_tokens": 4,  # we only want one digit
    }
    async with httpx.AsyncClient(
        base_url=cred.endpoint_url.rstrip("/"),
        timeout=timeout_s,
        headers={"Authorization": f"Bearer {cred.api_key}"},
    ) as client:
        try:
            r = await client.post("/chat/completions", json=body)
            r.raise_for_status()
        except (httpx.HTTPError, httpx.HTTPStatusError) as exc:
            logger.warning(
                "judge call failed (%s) — score skipped", type(exc).__name__
            )
            return None

    try:
        text = r.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        logger.warning("judge response shape unexpected — score skipped")
        return None

    m = _SCORE_RE.match(text or "")
    if m is None:
        logger.info("judge returned unparseable text %r — score skipped", text[:40])
        return None
    return int(m.group(1))
